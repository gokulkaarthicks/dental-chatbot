/**
 * `/staff/login` -- the dashboard sign-in screen.
 *
 * Credentials are not printed on the card: the board is reachable with the
 * clinic's own staff account, and the same green brand mark is used here as in
 * the patient chat so both interfaces read as one product.
 */

import { useEffect, useState, type FormEvent } from "react";
import { CircleDot } from "lucide-react";

import { Button } from "../../components/Button";
import { useClinicName } from "../../hooks/useClinicName";
import { useStaffAuth } from "./StaffAuthContext";

export function StaffLoginPage() {
  const { login, loggingIn, loginError, status } = useStaffAuth();
  // Same backend configuration as the patient chat: one name, two interfaces.
  const clinicName = useClinicName();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // Already signed in (e.g. came back via the back button): go to the board.
  useEffect(() => {
    if (status === "authenticated") {
      window.location.replace("/staff");
    }
  }, [status]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const ok = await login(username, password);
    if (ok) {
      window.location.replace("/staff");
    }
  }

  return (
    <div className="sd-login">
      <div className="sd-login__panel">
        <div className="sd-login__brand">
          <span className="sd-login__mark" aria-hidden="true">
            <CircleDot size={16} strokeWidth={2} />
          </span>
          <div>
            <p className="sd-login__eyebrow">{clinicName}</p>
            <h1 className="sd-login__title">Staff sign in</h1>
          </div>
        </div>

        <p className="sd-login__lede">
          Manage appointments, patient requests, and clinical alerts.
        </p>

        <form className="sd-login__form" onSubmit={onSubmit}>
          <label className="field">
            <span className="sd-field__label">Username</span>
            <input
              className="input"
              type="text"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              autoFocus
            />
          </label>

          <label className="field">
            <span className="sd-field__label">Password</span>
            <input
              className="input"
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>

          {loginError ? (
            <p className="sd-login__error" role="alert">
              {loginError}
            </p>
          ) : null}

          <Button type="submit" variant="primary" block disabled={loggingIn} className="sd-login__submit">
            {loggingIn ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </div>
    </div>
  );
}
