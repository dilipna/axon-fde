# Decorative motion

The overview now animates the illustrated delivery truck, wheel markers, cooling emblem, route dashes and a travelling signal highlight. These illustrate the concept; they do not generate readings, imply a live map or modify recorded evidence. The explicit illustration label is retained beside the motion control.

Motion starts only after JS initializes. A keyboard-operable pause/play button controls the illustration. Device reduced-motion preferences suppress all animation and disable the motion control with an explanatory title. Preference changes are handled while the page is open. Motion pauses offscreen through IntersectionObserver and in hidden tabs through visibilitychange. No persistent storage, external requests, model calls or tracking are added. Without JavaScript the illustration remains static and readable; the unavailable control stays hidden. Hover/focus elevation is limited to devices with hover and no reduced-motion preference.

Existing screenshots run in reduced-motion mode for deterministic review. Three added GitHub-only browser cases cover real movement at desktop/mobile widths, keyboard pause/resume, offscreen suspension, preference changes and initial reduced-motion behavior. Public release requires exact-source approval, CI/browser/screenshot gates and actual production verification. No local browser or desktop control.
