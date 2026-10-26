-- Keep the selected map place and explicit in-AOI measurement target with the durable job.
-- The target is not a stable reference; science stages still need independent common-datum evidence.
begin;

alter table public.displacement_jobs
  add column if not exists place_name text
    check (place_name is null or length(place_name) <= 200),
  add column if not exists place_latitude double precision
    check (place_latitude is null or (place_latitude >= -90 and place_latitude <= 90)),
  add column if not exists place_longitude double precision
    check (place_longitude is null or (place_longitude >= -180 and place_longitude <= 180)),
  add column if not exists target_latitude double precision
    check (target_latitude is null or (target_latitude >= -90 and target_latitude <= 90)),
  add column if not exists target_longitude double precision
    check (target_longitude is null or (target_longitude >= -180 and target_longitude <= 180));

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'displacement_jobs_place_coordinate_pair') then
    alter table public.displacement_jobs add constraint displacement_jobs_place_coordinate_pair
      check ((place_latitude is null) = (place_longitude is null));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'displacement_jobs_target_coordinate_pair') then
    alter table public.displacement_jobs add constraint displacement_jobs_target_coordinate_pair
      check ((target_latitude is null) = (target_longitude is null));
  end if;
end;
$$;

create or replace function public.enforce_displacement_place_immutable()
returns trigger language plpgsql set search_path = public as $$
begin
  if new.place_name is distinct from old.place_name
    or new.place_latitude is distinct from old.place_latitude
    or new.place_longitude is distinct from old.place_longitude
    or new.target_latitude is distinct from old.target_latitude
    or new.target_longitude is distinct from old.target_longitude
  then
    raise exception 'displacement place and target inputs are immutable';
  end if;
  return new;
end;
$$;

drop trigger if exists displacement_jobs_place_immutable on public.displacement_jobs;
create trigger displacement_jobs_place_immutable
before update on public.displacement_jobs
for each row execute function public.enforce_displacement_place_immutable();

comment on column public.displacement_jobs.place_name is
  'User-selected place context, not a scientific target by itself.';
comment on column public.displacement_jobs.target_latitude is
  'Optional in-AOI measurement target latitude; not a stable reference point.';
comment on column public.displacement_jobs.target_longitude is
  'Optional in-AOI measurement target longitude; not a stable reference point.';

commit;
