// MapLibre 6 renders GeoJSON layers in a module web worker that imports a shared chunk next to it.
// Vite does not bundle that worker, so ship both files as static assets under /maplibre/.
import { copyFileSync, mkdirSync } from "node:fs";

mkdirSync("public/maplibre", { recursive: true });
for (const f of ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"]) {
  copyFileSync(`node_modules/maplibre-gl/dist/${f}`, `public/maplibre/${f}`);
}
