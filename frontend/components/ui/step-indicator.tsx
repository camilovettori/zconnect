export type Step = {
  label: string;
  description?: string;
};

export type StepState = "complete" | "current" | "upcoming";

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5">
      <path d="m3.5 8.5 3 3 6-7" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
    </svg>
  );
}

// Presentational workflow indicator. `states` is supplied by the caller from
// existing page state, so this component holds no workflow logic of its own.
export function StepIndicator({
  steps,
  states,
  className = "",
}: {
  steps: Step[];
  states: StepState[];
  className?: string;
}) {
  return (
    <ol className={["grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4", className].join(" ")} aria-label="Workflow progress">
      {steps.map((step, index) => {
        const state = states[index] ?? "upcoming";
        return (
          <li
            key={step.label}
            aria-current={state === "current" ? "step" : undefined}
            className={[
              "flex items-start gap-3 border-t-2 pt-3",
              state === "complete" ? "border-brand-500" : state === "current" ? "border-brand-300" : "border-slate-200",
            ].join(" ")}
          >
            <span
              className={[
                "flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold tabular-nums",
                state === "complete"
                  ? "bg-brand-600 text-white"
                  : state === "current"
                    ? "border border-brand-300 bg-brand-50 text-brand-700"
                    : "border border-slate-200 bg-white text-slate-400",
              ].join(" ")}
            >
              {state === "complete" ? <CheckIcon /> : index + 1}
            </span>
            <span className="min-w-0">
              <span className={["block text-sm font-medium", state === "upcoming" ? "text-slate-500" : "text-slate-900"].join(" ")}>
                {step.label}
                <span className="sr-only">{state === "complete" ? " (completed)" : state === "current" ? " (current step)" : ""}</span>
              </span>
              {step.description && <span className="mt-0.5 block text-xs leading-5 text-slate-500">{step.description}</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
