import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { UNAUTHORIZED_EVENT, api } from "../api/client";

const MIN_PASSWORD = 8;

interface Auth {
  username: string;
  signOut: () => void;
}

const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth | null {
  return useContext(AuthContext);
}

/** Shows first setup or the login page until the admin is signed in. */
export function AuthGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { data, error, refetch } = useQuery({ queryKey: ["auth"], queryFn: api.authStatus, staleTime: Infinity });
  useEffect(() => {
    const onUnauthorized = () => refetch();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [refetch]);
  const signedIn = (username: string) => {
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
    qc.setQueryData(["auth"], { setup_required: false, setup_locked: false, user: { username } });
  };
  const signOut = useMutation({
    mutationFn: api.logout,
    onSettled: () => {
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
      qc.setQueryData(["auth"], { setup_required: false, setup_locked: false, user: null });
    },
  });

  if (error) return <AuthPage title="FlameKeeper"><p className="error-text">Can't reach FlameKeeper: {(error as Error).message}</p></AuthPage>;
  if (!data) return <AuthPage title="FlameKeeper"><p className="muted">Loading…</p></AuthPage>;
  if (data.setup_required && data.setup_locked) return <SetupLocked onRetry={() => refetch()} />;
  if (data.setup_required) return <Setup onDone={signedIn} />;
  if (!data.user) return <Login onDone={signedIn} />;
  return (
    <AuthContext.Provider value={{ username: data.user.username, signOut: () => signOut.mutate() }}>
      {children}
    </AuthContext.Provider>
  );
}

function AuthPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="auth-page">
      <div className="card auth-card">
        <div className="brand auth-brand">
          <svg width="18" height="18" viewBox="0 0 32 32" aria-hidden="true">
            <path d="M16 3c2 5 8 8 8 15a8 8 0 0 1-16 0c0-4 2-6 4-8 0 3 1 5 3 5-1-4 0-8 1-12z" fill="currentColor" />
          </svg>
          FlameKeeper
        </div>
        <h1>{title}</h1>
        {children}
      </div>
    </div>
  );
}

function SetupLocked({ onRetry }: { onRetry: () => void }) {
  return (
    <AuthPage title="Setup is locked">
      <p style={{ marginTop: 0 }}>
        Nobody created the admin account within 10 minutes of FlameKeeper starting, so setup from the browser is
        locked. That keeps an unclaimed install from being taken over by whoever finds it.
      </p>
      <p>On the server, either:</p>
      <ul>
        <li>
          create the account in the terminal: <span className="mono">sudo flamekeeper reset-admin</span>, or
        </li>
        <li>
          restart FlameKeeper with <span className="mono">sudo flamekeeper restart</span>, then reload this page within
          10 minutes.
        </li>
      </ul>
      <button className="btn" onClick={onRetry}>Check again</button>
    </AuthPage>
  );
}

function Setup({ onDone }: { onDone: (username: string) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const setup = useMutation({
    mutationFn: () => api.setup({ username, password }),
    onSuccess: (r) => onDone(r.user.username),
  });
  const mismatch = confirm.length > 0 && confirm !== password;
  const short = password.length > 0 && password.length < MIN_PASSWORD;
  const ready = username.trim() && password.length >= MIN_PASSWORD && confirm === password;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (ready) setup.mutate();
  };
  return (
    <AuthPage title="Create the admin account">
      <p className="muted" style={{ marginTop: 0 }}>
        Welcome! Choose the username and password you'll sign in with.
      </p>
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="a-user">Username</label>
        <input id="a-user" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        <label htmlFor="a-pass">Password</label>
        <input id="a-pass" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} aria-invalid={short} />
        {short && <span className="field-error">At least {MIN_PASSWORD} characters.</span>}
        <label htmlFor="a-confirm">Confirm password</label>
        <input id="a-confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-invalid={mismatch} />
        {mismatch && <span className="field-error">The passwords don't match.</span>}
        {setup.error && <p className="error-text">{(setup.error as Error).message}</p>}
        <button className="btn primary" type="submit" disabled={!ready || setup.isPending}>
          {setup.isPending ? "Creating…" : "Create account"}
        </button>
      </form>
    </AuthPage>
  );
}

function Login({ onDone }: { onDone: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => api.login(username, password),
    onSuccess: (r) => onDone(r.user.username),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (username && password) login.mutate();
  };
  return (
    <AuthPage title="Sign in">
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="l-user">Username</label>
        <input id="l-user" autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        <label htmlFor="l-pass">Password</label>
        <input id="l-pass" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {login.error && <p className="error-text">{(login.error as Error).message}</p>}
        <button className="btn primary" type="submit" disabled={!username || !password || login.isPending}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="muted small" style={{ marginBottom: 0 }}>
        Forgot the password? Run <span className="mono">sudo flamekeeper reset-admin</span> on the server to set a new
        one.
      </p>
    </AuthPage>
  );
}
