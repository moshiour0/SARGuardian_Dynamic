import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const pythonSource = readFileSync(new URL("../app/main.py", import.meta.url), "utf8");
const match = pythonSource.match(/DASHBOARD_GOOGLE_MAPS_ADAPTER_SCRIPT = r"""<script>\s*([\s\S]*?)\s*<\/script>"""/);
assert.ok(match, "Google Maps adapter script must remain embedded in the backend response");

class FakeMap {
  constructor(container, options) {
    this.container = container;
    this.options = options;
    this.zoom = Number(options.zoom) || 2;
    this.center = options.center;
  }
  fitBounds(bounds, padding) { this.fittedBounds = bounds; this.padding = padding; }
  getCenter() {
    const value = this.center;
    return {
      lat: () => typeof value.lat === "function" ? value.lat() : Number(value.lat),
      lng: () => typeof value.lng === "function" ? value.lng() : Number(value.lng),
    };
  }
  getZoom() { return this.zoom; }
  getHeading() { return 0; }
  getTilt() { return 0; }
  setZoom(value) { this.zoom = value; }
  setCenter(value) { this.center = value; }
  setOptions(value) { Object.assign(this.options, value); if (value.zoom != null) this.zoom = value.zoom; if (value.center) this.center = value.center; }
}

class FakePolygon {
  static instances = [];
  constructor(options) {
    this.options = options;
    this.map = null;
    this.listeners = new Map();
    FakePolygon.instances.push(this);
  }
  setMap(map) { this.map = map; }
  addListener(name, callback) {
    const entry = { callback, removed: false, remove() { this.removed = true; } };
    const list = this.listeners.get(name) || [];
    list.push(entry);
    this.listeners.set(name, list);
    return entry;
  }
  fire(name, event) { (this.listeners.get(name) || []).forEach((entry) => entry.callback(event)); }
}

class FakeMarker {
  constructor(options) { this.options = options; this.map = null; }
  setMap(map) { this.map = map; }
}
class FakeGroundOverlay {
  constructor(url, bounds, options) { this.url = url; this.bounds = bounds; this.options = options; this.map = null; }
  setMap(map) { this.map = map; }
  setOpacity(value) { this.opacity = value; }
}
class FakeInfoWindow {
  static instances = [];
  constructor(options) { this.options = options; this.content = ""; this.position = null; this.openedOn = null; FakeInfoWindow.instances.push(this); }
  setContent(value) { this.content = value; }
  setPosition(value) { this.position = value; }
  open(options) { this.openedOn = options.map; }
  close() { this.openedOn = null; }
}
class FakeLatLngBounds {
  constructor(value) { this.value = value; }
}

const google = {
  maps: {
    Map: FakeMap,
    Polygon: FakePolygon,
    Marker: FakeMarker,
    GroundOverlay: FakeGroundOverlay,
    InfoWindow: FakeInfoWindow,
    LatLngBounds: FakeLatLngBounds,
    MapTypeId: { TERRAIN: "terrain" },
    MapTypeControlStyle: { DROPDOWN_MENU: "dropdown" },
    SymbolPath: { CIRCLE: 0 },
    event: {
      addListenerOnce(_target, name, callback) { if (name === "idle") callback(); return { remove() {} }; },
      addListener(_target, _name, _callback) { return { remove() {} }; },
      trigger() {},
    },
  },
};

const windowObject = {
  google,
  addEventListener() {},
};
const documentObject = {
  createElement() { return {}; },
  getElementById() { return { id: "map" }; },
  head: {
    appendChild() {
      queueMicrotask(() => windowObject.__sarguardianGoogleMapsLoaded());
    },
  },
};
const context = { google, window: windowObject, document: documentObject, console };
context.globalThis = context;
vm.runInNewContext(match[1], context, { filename: "google-maps-dashboard-adapter.js" });
await new Promise((resolve) => setImmediate(resolve));
const compatibility = await windowObject.SARGUARDIAN_GOOGLE_MAPS_ADAPTER;
assert.equal(typeof compatibility.Map, "function");
assert.equal(typeof compatibility.Popup, "function");

FakePolygon.instances = [];
FakeInfoWindow.instances = [];
const map = new compatibility.Map({ container: "map", center: [85.3, 27.7], zoom: 6 });
assert.equal(map.loaded(), true);

const featureCollection = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: "goff-one",
      properties: { candidate_id: "GOFF-C001", product_type: "GOFF" },
      geometry: { type: "Polygon", coordinates: [[[85.30, 27.70], [85.31, 27.70], [85.31, 27.71], [85.30, 27.70]]] },
    },
    {
      type: "Feature",
      id: "gunw-multipolygon",
      properties: { candidate_id: "GUNW-C002", product_type: "GUNW", status: "REPEATED_SPATIAL_CANDIDATE" },
      geometry: {
        type: "MultiPolygon",
        coordinates: [
          [[ [85.32, 27.70], [85.33, 27.70], [85.33, 27.71], [85.32, 27.70] ]],
          [[ [85.34, 27.70], [85.35, 27.70], [85.35, 27.71], [85.34, 27.70] ]],
        ],
      },
    },
    {
      type: "Feature",
      id: "ignored-point",
      properties: { candidate_id: "POINT-IGNORED" },
      geometry: { type: "Point", coordinates: [85.36, 27.72] },
    },
  ],
};

map.addSource("candidate-source", { type: "geojson", data: featureCollection });
map.addLayer({
  id: "aoi-wide-candidate-fill",
  type: "fill",
  source: "candidate-source",
  filter: ["==", ["geometry-type"], "Polygon"],
  paint: {
    "fill-color": [
      "case",
      ["==", ["get", "product_type"], "GOFF"], "#7dd3fc",
      ["==", ["get", "status"], "REPEATED_SPATIAL_CANDIDATE"], "#ff6b6b",
      "#e8b24b",
    ],
    "fill-opacity": 0.28,
  },
});

const layer = map.getLayer("aoi-wide-candidate-fill");
const clicked = [];
map.on("click", "aoi-wide-candidate-fill", (event) => {
  clicked.push(event);
  new compatibility.Popup({ maxWidth: 320 })
    .setLngLat(event.lngLat)
    .setHTML("<strong>candidate details</strong>")
    .addTo(map);
});

assert.equal(layer.overlays.length, 3, "all polygon features and both MultiPolygon components must be rendered");
assert.deepEqual(FakePolygon.instances.map((item) => item.options.fillColor), ["#7dd3fc", "#ff6b6b", "#ff6b6b"]);
assert.ok(FakePolygon.instances.every((item) => item.options.clickable === true));

FakePolygon.instances[1].fire("click", {
  latLng: { lat: () => 27.705, lng: () => 85.325 },
  domEvent: { type: "click" },
});
assert.equal(clicked.length, 1);
assert.equal(clicked[0].features[0].properties.candidate_id, "GUNW-C002");
assert.equal(clicked[0].lngLat.lat, 27.705);
assert.equal(clicked[0].lngLat.lng, 85.325);
assert.equal(FakeInfoWindow.instances.length, 1);
assert.equal(FakeInfoWindow.instances[0].content, "<strong>candidate details</strong>");
assert.equal(FakeInfoWindow.instances[0].position.lat, 27.705);
assert.equal(FakeInfoWindow.instances[0].position.lng, 85.325);
assert.equal(FakeInfoWindow.instances[0].openedOn, map.map);

map.moveLayer("aoi-wide-candidate-fill");
map.setLayoutProperty("aoi-wide-candidate-fill", "visibility", "none");
assert.ok(FakePolygon.instances.every((item) => item.map === null));
map.setLayoutProperty("aoi-wide-candidate-fill", "visibility", "visible");
assert.ok(FakePolygon.instances.every((item) => item.map === map.map));
map.removeLayer("aoi-wide-candidate-fill");
assert.ok(FakePolygon.instances.every((item) => item.map === null));
assert.ok(FakePolygon.instances.every((item) => item.listeners.get("click")[0].removed));

console.log("GOOGLE_MAPS_DASHBOARD_ADAPTER_PASS=multi-feature;MultiPolygon;feature-style;layer-click;InfoWindow;visibility;cleanup");
