// Shared class names of the V2 design language (same values as the Sync and History pages).

const BUTTON_SHAPE =
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none disabled:cursor-not-allowed disabled:opacity-50";

export const BUTTON_PRIMARY = `${BUTTON_SHAPE} bg-brand-600 text-white shadow-card hover:bg-brand-700 disabled:hover:bg-brand-600`;
export const BUTTON_SECONDARY = `${BUTTON_SHAPE} border border-slate-200 bg-white text-slate-700 shadow-card hover:border-slate-300 hover:bg-slate-50 hover:text-slate-950 disabled:hover:bg-white`;
export const BUTTON_GHOST = `${BUTTON_SHAPE} text-slate-600 hover:bg-slate-100 hover:text-slate-950 disabled:hover:bg-transparent`;
export const BUTTON_DANGER = `${BUTTON_SHAPE} border border-rose-200 bg-white text-rose-700 shadow-card hover:border-rose-300 hover:bg-rose-50 disabled:hover:bg-white`;
export const BUTTON_DANGER_SOLID = `${BUTTON_SHAPE} bg-rose-600 text-white shadow-card hover:bg-rose-700 disabled:hover:bg-rose-600`;

export const CARD = "rounded-2xl border border-slate-200/80 bg-white shadow-card";

export const INPUT =
  "h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-900 shadow-card placeholder:text-slate-400 focus:border-brand-400 focus:outline-none focus:ring-4 focus:ring-brand-100 disabled:bg-slate-50 disabled:text-slate-500";

export const LABEL = "mb-1.5 block text-sm font-medium text-slate-700";
