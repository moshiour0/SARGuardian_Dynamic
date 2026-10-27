import type { Coordinate } from './types';
import { validateCatalogPreflight } from './validation';

// Map state is a single ring of [longitude, latitude] pairs, not Google LatLng objects.
// Nested rings, holes and multipolygons are deliberately rejected.
export function polygonToWkt(value: unknown): string {
  if (!Array.isArray(value) || !value.every((point) => Array.isArray(point) && point.length === 2
    && point.every((n) => typeof n === 'number' && Number.isFinite(n))
    && Math.abs(point[0]) <= 180 && Math.abs(point[1]) <= 90)) {
    throw new Error('AOI must be a single polygon of valid longitude/latitude pairs.');
  }
  const points = value as Coordinate[];
  if (new Set(points.map(([lng, lat]) => `${lng},${lat}`)).size < 3) {
    throw new Error('AOI needs at least three distinct vertices.');
  }
  const first = points[0];
  const last = points[points.length - 1];
  const ring = first[0] === last[0] && first[1] === last[1] ? points : [...points, first];
  // JS number serialization round-trips without additional rounding.
  return `POLYGON((${ring.map(([lng, lat]) => `${lng} ${lat}`).join(', ')}))`;
}

// Parse only a single-ring WGS84 POLYGON; geometry with holes or multiple parts is rejected.
export function parsePolygonWkt(value: string): Coordinate[] {
  const match = /^\s*POLYGON\s*\(\s*\(([^()]*)\)\s*\)\s*$/i.exec(value);
  if (!match) throw new Error('Enter one WGS84 POLYGON ((longitude latitude, ...)); holes and MULTIPOLYGON are not supported.');
  const points = match[1].split(',').map((pair): Coordinate => {
    const values = pair.trim().split(/\s+/);
    const numberPattern = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
    if (values.length !== 2 || values.some((item) => !numberPattern.test(item))) {
      throw new Error('Each WKT vertex must contain exactly two valid numeric values: longitude and latitude.');
    }
    const longitude = Number(values[0]);
    const latitude = Number(values[1]);
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)
      || longitude < -180 || longitude > 180 || latitude < -90 || latitude > 90) {
      throw new Error('WKT vertices must be finite WGS84 longitude/latitude values.');
    }
    return [longitude, latitude];
  });
  // WKT LinearRings must be explicitly closed; do not silently repair malformed input.
  if (points.length < 4 || points[0][0] !== points[points.length - 1][0]
    || points[0][1] !== points[points.length - 1][1]) {
    throw new Error('AOI WKT polygon ring must be closed by repeating its first longitude/latitude vertex.');
  }
  points.pop();
  if (new Set(points.map(([longitude, latitude]) => `${longitude},${latitude}`)).size < 3) {
    throw new Error('AOI WKT needs at least three distinct vertices.');
  }
  // Reuse the same strict geometry validation as the interactive-map path.
  polygonToWkt(points);
  return points;
}

export type DisplacementPayload = {
  aoi_wkt: string;
  start_date: string;
  end_date: string;
  place_name: string | null;
  place_latitude: number | null;
  place_longitude: number | null;
  target_latitude: number | null;
  target_longitude: number | null;
};
export type InvestigationSelection = Parameters<typeof validateCatalogPreflight>[0];
export type PreparedInvestigation = { valid: true; payload: DisplacementPayload } | { valid: false; errors: string[] };
export type PlaceContext = { name: string; latitude: number; longitude: number } | null;

export function isPointInsidePolygon(longitude: number, latitude: number, ring: Coordinate[]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    // A point on a polygon edge is treated as inside for target eligibility.
    const cross = (longitude - xi) * (yj - yi) - (latitude - yi) * (xj - xi);
    const onSegment = Math.abs(cross) < 1e-10
      && longitude >= Math.min(xi, xj) - 1e-10 && longitude <= Math.max(xi, xj) + 1e-10
      && latitude >= Math.min(yi, yj) - 1e-10 && latitude <= Math.max(yi, yj) + 1e-10;
    if (onSegment) return true;
    const crosses = ((yi > latitude) !== (yj > latitude))
      && longitude < ((xj - xi) * (latitude - yi)) / ((yj - yi) || Number.EPSILON) + xi;
    if (crosses) inside = !inside;
  }
  return inside;
}

export function prepareInvestigation(input: InvestigationSelection & { place?: PlaceContext }): PreparedInvestigation {
  let wkt: string;
  try { wkt = polygonToWkt(input.coordinates); }
  catch (error) { return { valid: false, errors: [(error as Error).message] }; }
  const isDate = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(`${value}T00:00:00Z`))
    && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
  if (!isDate(input.startDate) || !isDate(input.endDate)) {
    return { valid: false, errors: ['Choose valid start and end dates (YYYY-MM-DD).'] };
  }
  const validation = validateCatalogPreflight(input);
  if (!validation.valid) return validation;
  const place = input.place;
  const hasValidPlace = place !== null && place !== undefined
    && Number.isFinite(place.latitude) && Number.isFinite(place.longitude)
    && place.latitude >= -90 && place.latitude <= 90
    && place.longitude >= -180 && place.longitude <= 180;
  const selectedPointIsInside = Boolean(hasValidPlace && isPointInsidePolygon(
    place!.longitude, place!.latitude, input.coordinates,
  ));
  return { valid: true, payload: {
    aoi_wkt: wkt,
    start_date: input.startDate,
    end_date: input.endDate,
    place_name: hasValidPlace ? place!.name.trim().slice(0, 200) || null : null,
    place_latitude: hasValidPlace ? place!.latitude : null,
    place_longitude: hasValidPlace ? place!.longitude : null,
    // A searched place is a measurement target only when the point is inside the
    // exact AOI polygon. Location context alone must never imply an in-AOI sample.
    target_latitude: selectedPointIsInside ? place!.latitude : null,
    target_longitude: selectedPointIsInside ? place!.longitude : null,
  } };
}

export async function copyPreparation(payload: DisplacementPayload, kind: 'wkt' | 'payload', writeText: (text: string) => Promise<void>): Promise<void> {
  await writeText(kind === 'wkt' ? payload.aoi_wkt : JSON.stringify(payload, null, 2));
}
