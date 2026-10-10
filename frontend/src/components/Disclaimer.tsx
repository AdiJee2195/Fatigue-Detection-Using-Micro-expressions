/**
 * Scientific honesty and privacy, stated once and permanently on screen.
 *
 * These are not legal boilerplate for this project: the system produces a *risk
 * indicator* from a camera and a small, frame-level classifier, and the distance
 * between that and a validated safety device is exactly what a reader must not
 * overestimate.
 */
export function Disclaimer() {
  return (
    <footer className="border-t border-slate-800 bg-slate-900/60 px-4 py-4">
      <div className="mx-auto grid max-w-[1600px] gap-4 text-xs text-slate-400 md:grid-cols-3">
        <section aria-labelledby="disclaimer-research">
          <h2 id="disclaimer-research" className="mb-1 font-semibold text-slate-300">
            Research prototype
          </h2>
          <p>
            This system is <strong className="text-amber-200">not a medical device</strong> and
            must not be used as the sole basis for driving or safety decisions. It reports a risk
            indicator from behavioural signals, not a diagnosis of fatigue, and it has not been
            clinically validated.
          </p>
        </section>

        <section aria-labelledby="disclaimer-scope">
          <h2 id="disclaimer-scope" className="mb-1 font-semibold text-slate-300">
            What is and is not implemented
          </h2>
          <p>
            The deployed classifier is a frame-level 3-class model, not the temporal
            architecture discussed in the literature. Eye openness is measured where a landmark
            model is available; action-unit intensities and remote heart rate are not
            implemented in this deployment and are reported as such rather than estimated.
          </p>
        </section>

        <section aria-labelledby="disclaimer-privacy">
          <h2 id="disclaimer-privacy" className="mb-1 font-semibold text-slate-300">
            Privacy
          </h2>
          <p>
            Frames are processed transiently and are never written to disk, never stored in a
            database and never sent to a language model. Only numeric telemetry leaves the vision
            service. Nothing on this page is retained after the session ends.
          </p>
        </section>
      </div>
    </footer>
  );
}
