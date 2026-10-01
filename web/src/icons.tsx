// Small inline stroke icons (no icon font, no extra dependency).
const P = {
  layers: "m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
  map: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Zm0 0v14m6-12v14",
  bell: "M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8Zm4 12a2 2 0 0 0 4 0",
  trophy: "M8 21h8m-4-4v4M7 4h10v5a5 5 0 0 1-10 0V4Zm0 2H4a3 3 0 0 0 3 3m10-3h3a3 3 0 0 1-3 3",
  convoy: "M3 16h2m4 0h2m4 0h2M5 16a2 2 0 1 0 4 0 2 2 0 1 0-4 0Zm8 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M3 16V9l3-4h8l3 4h3v7",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  chart: "M4 20V10m6 10V4m6 16v-7m4 7H2",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.7-5.7 1.4-1.4M4.9 19.1l1.4-1.4m0-11.4L4.9 4.9m14.2 14.2-1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  moon: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10Z",
  logout: "M15 17l5-5-5-5m5 5H9m4 9H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8",
  tow: "M3 17h2m10 0h2M5 17a2 2 0 1 0 4 0 2 2 0 1 0-4 0Zm8 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M3 17v-5h10l3-4h3l2 4v5M14 4l-3 8",
  fence: "M12 21s-7-6-7-11a7 7 0 1 1 14 0c0 5-7 11-7 11Zm0-8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  tamper: "M12 9v4m0 4h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  route: "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm12-10a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM8 17h7a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h7",
  speed: "M12 14l4-4M4 18a9 9 0 1 1 16 0",
} as const;

export type IconName = keyof typeof P;

export function Icon({ name }: { name: IconName }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d={P[name]} />
    </svg>
  );
}

/** Icon + plain-English sentence for each detector signal. */
export const SIGNAL_INFO: Record<string, { icon: IconName; text: string }> = {
  TOW_SUSPECTED: { icon: "tow", text: "Moving with the engine off" },
  GEOFENCE_EXIT: { icon: "fence", text: "Left the permitted region" },
  TAMPER_SUSPECTED: { icon: "tamper", text: "GPS jumped, then went silent" },
  CONVOY: { icon: "convoy", text: "Travelling in a group" },
  ROUTE_DEVIATION: { icon: "route", text: "Far off its usual routes" },
  NIGHT_MOVEMENT: { icon: "moon", text: "Moving at an unusual hour" },
  SPEED_SPIKE: { icon: "speed", text: "Unusually high speed" },
};
