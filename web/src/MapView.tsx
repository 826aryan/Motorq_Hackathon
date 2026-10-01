import * as maplibregl from "maplibre-gl";
import type { Map as MLMap } from "maplibre-gl";
import { useEffect, useRef } from "react";

// See scripts/copy-maplibre-worker.mjs: the worker is served as a static file.
maplibregl.setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");

// Muted vector basemaps from OpenFreeMap (OSM data, free, no API key): quiet greys so vehicles and alerts
// stand out; the dark one follows the app's dark theme. Fine for a demo; self-host tiles before real traffic.
const STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const currentStyle = () => (document.documentElement.dataset.theme === "dark" ? STYLES.dark : STYLES.light);

export const CENTER: [number, number] = [77.5946, 12.9716];

export function MapView({ onReady, zoom = 11.5 }: { onReady: (m: MLMap) => void | (() => void); zoom?: number }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const map = new maplibregl.Map({ container: el.current!, style: currentStyle(), center: CENTER, zoom });
    map.addControl(new maplibregl.NavigationControl(), "top-left");
    let cleanup: void | (() => void);
    map.on("load", () => {
      cleanup = onReady(map);
    });
    return () => {
      if (cleanup) cleanup();
      map.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div ref={el} className="map" />;
}

export function emptyFC(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

export function pointsFC(items: { lat: number; lon: number; [k: string]: unknown }[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: items.map((it) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [it.lon, it.lat] },
      properties: { ...it },
    })),
  };
}

export function lineFC(coords: number[][]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: coords.length > 1 ? [{ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} }] : [],
  };
}

export function setData(map: MLMap, source: string, data: GeoJSON.FeatureCollection) {
  (map.getSource(source) as maplibregl.GeoJSONSource | undefined)?.setData(data);
}

/** Draw the lender's geofence polygons (outline + light fill). */
export function addGeofences(map: MLMap, fences: { area: GeoJSON.Polygon }[]) {
  map.addSource("fences", {
    type: "geojson",
    data: {
      type: "FeatureCollection",
      features: fences.map((f) => ({ type: "Feature", geometry: f.area, properties: {} })),
    },
  });
  // A soft tinted zone with a thin edge: visible, but quieter than the vehicles on top of it.
  map.addLayer({ id: "fence-fill", type: "fill", source: "fences", paint: { "fill-color": "#3b82f6", "fill-opacity": 0.06 } });
  map.addLayer({ id: "fence-line", type: "line", source: "fences",
    paint: { "line-color": "#3b82f6", "line-width": 1.5, "line-opacity": 0.55 } });
}

export const RISK_COLOR_EXPR: maplibregl.ExpressionSpecification = [
  "step", ["get", "risk"], "#2f7d4f", 0.01, "#d8b21c", 40, "#e0801f", 70, "#c0262d",
];
