-- Immutable scientific request options and callback binding for contract v2.
begin;

alter table public.displacement_jobs
  add column if not exists product_type text not null default 'BOTH'
    check (product_type in ('BOTH', 'GUNW', 'GOFF')),
  add column if not exists acquisition_window text not null default 'secondary-acquisition-in-window'
    check (acquisition_window in ('secondary-acquisition-in-window', 'both-acquisitions-in-window')),
  add column if not exists retention_mode text not null default 'derived-only'
    check (retention_mode in ('derived-only', 'full-lossless')),
  add column if not exists processing_profile text not null default 'validation-pilot'
    check (processing_profile in ('validation-pilot', 'full-aoi')),
  add column if not exists request_fingerprint text
    check (request_fingerprint is null or request_fingerprint ~ '^[a-f0-9]{64}$'),
  add column if not exists worker_contract_version integer
    check (worker_contract_version is null or worker_contract_version = 2);

drop index if exists public.displacement_jobs_one_active_request_per_owner_idx;
create unique index displacement_jobs_one_active_request_per_owner_idx
  on public.displacement_jobs (
    owner_uid, md5(aoi_wkt), start_date, end_date,
    product_type, acquisition_window, retention_mode, processing_profile
  ) where state in ('queued', 'dispatching', 'running');

-- Free-tier admission control: a user may not accumulate distinct expensive
-- AOI requests simply by changing one coordinate or option in each request.
create unique index if not exists displacement_jobs_one_active_per_owner_idx
  on public.displacement_jobs (owner_uid)
  where state in ('queued', 'dispatching', 'running');

create or replace function public.enforce_displacement_job_transition()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op = 'INSERT' then
    if new.state <> 'queued' then raise exception 'new displacement jobs must start queued'; end if;
    if new.worker_contract_version = 2 and new.request_fingerprint is null then
      raise exception 'version-2 displacement jobs require a request fingerprint';
    end if;
    return new;
  end if;
  if new.state <> old.state and not (
    (old.state = 'queued' and new.state in ('dispatching', 'failed', 'cancelled')) or
    (old.state = 'dispatching' and new.state in ('running', 'failed', 'cancelled')) or
    (old.state = 'running' and new.state in ('succeeded', 'failed', 'cancelled'))
  ) then raise exception 'invalid displacement job transition: % -> %', old.state, new.state; end if;
  if new.owner_uid is distinct from old.owner_uid
    or new.idempotency_key is distinct from old.idempotency_key
    or new.aoi_wkt is distinct from old.aoi_wkt
    or new.start_date is distinct from old.start_date
    or new.end_date is distinct from old.end_date
    or new.product_type is distinct from old.product_type
    or new.acquisition_window is distinct from old.acquisition_window
    or new.retention_mode is distinct from old.retention_mode
    or new.processing_profile is distinct from old.processing_profile
    or new.request_fingerprint is distinct from old.request_fingerprint
    or new.worker_contract_version is distinct from old.worker_contract_version
  then raise exception 'displacement request inputs are immutable'; end if;
  if new.callback_sequence < old.callback_sequence or new.callback_sequence > old.callback_sequence + 1 then
    raise exception 'displacement callback sequence is invalid';
  end if;
  new.updated_at = now();
  return new;
end;
$$;

comment on column public.displacement_jobs.request_fingerprint is
  'SHA-256 binding of canonical AOI, date policy, processing options, science commit and output contract.';
commit;
