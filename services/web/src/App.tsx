import Hls from "hls.js";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  completeVideo,
  createVideo,
  getVideo,
  listVideos,
  login,
  logout,
  putFile,
  session,
  signup,
  type Job,
} from "./api";

const ALLOWED = new Set(["video/mp4", "video/quicktime", "video/webm"]);

export function App() {
  const [csrf, setCsrf] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [address, setAddress] = useState("");
  const [password, setPassword] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [active, setActive] = useState<Job | null>(null);

  useEffect(() => {
    session()
      .then((value) => {
        setEmail(value.user.email);
        setCsrf(value.csrf);
      })
      .catch(() => setCsrf(null));
  }, []);

  useEffect(() => {
    if (!csrf) return;
    listVideos().then(setJobs).catch((err: Error) => setError(err.message));
  }, [csrf]);

  useEffect(() => {
    if (!active || !["awaiting_upload", "uploaded", "transcoding"].includes(active.status)) return;
    const timer = window.setInterval(() => {
      getVideo(active.id)
        .then((job) => {
          setActive(job);
          setJobs((current) => current.map((item) => (item.id === job.id ? job : item)));
        })
        .catch((err: Error) => setError(err.message));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active?.id, active?.status]);

  async function onAuth(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mode === "signup") await signup(address, password);
      const value = await login(address, password);
      setEmail(value.user.email);
      setCsrf(value.csrf);
      setPassword("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  async function onUpload(event: FormEvent) {
    event.preventDefault();
    if (!file || !csrf) return;
    if (!ALLOWED.has(file.type) || file.size === 0) {
      setError("Choose an mp4, mov, or webm that is not empty.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await createVideo(csrf, crypto.randomUUID(), file);
      if (created.upload_url) await putFile(created.upload_url, file);
      const job = await completeVideo(csrf, created.job.id);
      setActive(job);
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setFile(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function onLogout() {
    if (!csrf) return;
    await logout(csrf);
    setCsrf(null);
    setJobs([]);
    setActive(null);
  }

  if (!csrf) {
    return (
      <main>
        <h1>Transcode</h1>
        <form onSubmit={onAuth}>
          <label>
            Email
            <input value={address} onChange={(event) => setAddress(event.target.value)} type="email" required />
          </label>
          <label>
            Password
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" minLength={8} required />
          </label>
          <button disabled={busy} type="submit">
            {mode === "login" ? "Log in" : "Create account"}
          </button>
          <button
            type="button"
            className="text"
            onClick={() => setMode(mode === "login" ? "signup" : "login")}
          >
            {mode === "login" ? "Need an account?" : "Already have an account?"}
          </button>
        </form>
        {error && <p className="error">{error}</p>}
      </main>
    );
  }

  return (
    <main>
      <header>
        <h1>Transcode</h1>
        <p>{email}</p>
        <button type="button" onClick={onLogout}>Log out</button>
      </header>
      <form onSubmit={onUpload}>
        <label>
          Video
          <input
            type="file"
            accept="video/mp4,video/quicktime,video/webm"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <button disabled={busy || !file} type="submit">
          {busy ? "Uploading" : "Upload"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {active && <Status job={active} />}
      <ul>
        {jobs.map((job) => (
          <li key={job.id}>
            <button type="button" onClick={() => setActive(job)}>
              {job.filename}
            </button>
            <span>{job.status}</span>
          </li>
        ))}
      </ul>
    </main>
  );
}

function Status({ job }: { job: Job }) {
  if (job.status === "failed" || job.status === "expired") {
    return <p className="error">{job.error || job.status}</p>;
  }
  if (job.status !== "ready" || !job.playlist_url) {
    return <p className="pill">{job.status.replace("_", " ")}</p>;
  }
  return <Player src={job.playlist_url} />;
}

function Player({ src }: { src: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (Hls.isSupported()) {
      const hls = new Hls({ xhrSetup: (xhr) => { xhr.withCredentials = true; } });
      hls.loadSource(src);
      hls.attachMedia(video);
      return () => hls.destroy();
    }
    video.src = src;
  }, [src]);
  return <video ref={videoRef} controls playsInline />;
}
