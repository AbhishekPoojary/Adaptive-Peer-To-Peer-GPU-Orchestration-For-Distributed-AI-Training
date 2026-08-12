import { useEffect, useRef, useState } from "react";

/**
 * Google Identity Services sign-in button (ADR-012 addendum).
 *
 * Uses the ID-token flow, not the authorization-code redirect flow: Google hands
 * this page a signed ID token, we forward it to `POST /auth/google`, and the
 * orchestrator verifies it against Google's published keys. That means **no
 * client secret in the browser** (there is none to leak, unlike the
 * `VITE_ADMIN_API_KEY` mistake ADR-012 §6 removed) and **no redirect URI to
 * register**, which is what makes this workable for a deployment reached over a
 * Tailscale address rather than a stable public hostname.
 *
 * The script is loaded on demand rather than from index.html so a deployment
 * with Google sign-in switched off never contacts Google at all — and an
 * offline one degrades to a visible message beside a working password form,
 * instead of a button that silently does nothing.
 */

const GSI_SRC = "https://accounts.google.com/gsi/client";

/** Minimal shape of the GIS global we actually use. */
interface GoogleIdentityServices {
  accounts: {
    id: {
      initialize(config: {
        client_id: string;
        callback: (response: { credential?: string }) => void;
      }): void;
      renderButton(
        parent: HTMLElement,
        options: Record<string, string | number>,
      ): void;
    };
  };
}

declare global {
  interface Window {
    google?: GoogleIdentityServices;
  }
}

/** Load the GIS script once per page, reusing the in-flight promise. */
let scriptPromise: Promise<void> | null = null;

function loadGsiScript(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve();
  if (scriptPromise) return scriptPromise;

  scriptPromise = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${GSI_SRC}"]`,
    );
    const script = existing ?? document.createElement("script");
    script.addEventListener("load", () => resolve());
    script.addEventListener("error", () => {
      // Let a later mount retry: a failed load is usually "no internet right
      // now", which can stop being true without a page reload.
      scriptPromise = null;
      reject(new Error("could not load Google sign-in"));
    });
    if (!existing) {
      script.src = GSI_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
  });
  return scriptPromise;
}

export interface GoogleSignInButtonProps {
  clientId: string;
  /** Called with the ID token Google issued. */
  onCredential: (credential: string) => void;
  /** Rendered instead of the button while the parent is exchanging the token. */
  disabled?: boolean;
}

export function GoogleSignInButton({
  clientId,
  onCredential,
  disabled = false,
}: GoogleSignInButtonProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [unavailable, setUnavailable] = useState(false);

  // Keep the latest callback in a ref so re-rendering the parent (which happens
  // on every keystroke in the password form beside it) does not tear down and
  // re-render Google's button.
  const onCredentialRef = useRef(onCredential);
  useEffect(() => {
    onCredentialRef.current = onCredential;
  }, [onCredential]);

  useEffect(() => {
    let cancelled = false;

    void loadGsiScript()
      .then(() => {
        if (cancelled || !containerRef.current) return;
        const gsi = window.google?.accounts?.id;
        if (!gsi) {
          setUnavailable(true);
          return;
        }
        gsi.initialize({
          client_id: clientId,
          callback: ({ credential }) => {
            // No credential means the user dismissed the prompt or Google
            // declined to issue one. Nothing to report and nothing to send.
            if (credential) onCredentialRef.current(credential);
          },
        });
        gsi.renderButton(containerRef.current, {
          type: "standard",
          theme: "outline",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          width: 320,
        });
      })
      .catch(() => {
        if (!cancelled) setUnavailable(true);
      });

    return () => {
      cancelled = true;
    };
  }, [clientId]);

  if (unavailable) {
    return (
      <p className="text-xs text-tertiary">
        Google sign-in couldn&apos;t load — this machine may be offline. Use your
        username and password.
      </p>
    );
  }

  return (
    <div
      // Google renders an iframe here that ignores pointer-events styling, so a
      // disabled state is enforced by covering it rather than by dimming it.
      className={disabled ? "pointer-events-none opacity-60" : undefined}
      aria-busy={disabled}
    >
      <div ref={containerRef} />
    </div>
  );
}
