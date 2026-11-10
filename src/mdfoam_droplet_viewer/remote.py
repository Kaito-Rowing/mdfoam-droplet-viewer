"""SSH/SFTP access: browse remote folders, find cases, mirror only the files needed for analysis.

Files are read with SFTP. For bulk downloads the only remote command ever run is a read-only
`tar -c` that streams the requested files (one stream instead of one round trip per file); it is
used only when GNU tar is available and can be switched off. Credentials are kept in memory and
never written to settings or result files. Host aliases, users, ports and keys from ~/.ssh/config
are honoured.

paramiko's SFTPClient is not thread-safe: interactive calls (browse/scan) share one channel under a
lock, and mirroring uses its own pool of channels (one per worker thread) on the same connection.
"""

from __future__ import annotations

import json
import os
import posixpath
import queue
import shlex
import stat
import tarfile
import threading
from contextlib import contextmanager
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import paramiko

MESH_FILES = ("points", "faces", "owner", "neighbour", "boundary", "cellZones", "cellProcAddressing")
CONST_FILES = ("moleculeProperties", "idList")
META_FILES = ("case_info.txt", "batch_status.txt")
LAGRANGIAN_FILES = ("positions", "id", "origId", "origProcId")
# sshd allows 10 sessions per connection by default (MaxSessions); keep room for the interactive channel
WORKERS = 6        # channels per connection
CONNECTIONS = 2    # parallel SSH connections (kept low: servers often rate-limit new connections)
SYNC_MANIFEST = ".mdfoam_sync.json"
SYSTEM_MAX_BYTES = 64 * 1024  # system/ dictionaries (thermostat, initial state, run control) up to this size
TAR_CHUNK = 2000   # files per tar stream (several streams run in parallel)
TAR_STREAMS = 2    # tar streams per connection (sshd MaxSessions 10: 6 SFTP + interactive + these)
NOT_KEYS = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "environment"}


@dataclass
class RemoteConfig:
    host: str
    port: int = 0          # 0 -> from ~/.ssh/config (or 22)
    username: str = ""
    key_file: str = ""

    def public(self) -> dict:
        return {"host": self.host, "port": self.port, "username": self.username, "key_file": self.key_file}


def ssh_config_hosts() -> dict:
    """Host aliases from ~/.ssh/config (resolved values) and private-key file *names* in ~/.ssh.

    Key contents are never read here. PuTTY .ppk files are listed separately because paramiko
    cannot use them (convert with PuTTYgen -> Export OpenSSH key).
    """
    ssh_dir = Path.home() / ".ssh"
    hosts = []
    cfg_path = ssh_dir / "config"
    if cfg_path.exists():
        cfg = paramiko.SSHConfig()
        with cfg_path.open(encoding="utf-8") as fh:
            cfg.parse(fh)
        for alias in sorted(cfg.get_hostnames()):
            if any(ch in alias for ch in "*?!"):
                continue
            hc = cfg.lookup(alias)
            hosts.append({"alias": alias, "hostname": hc.get("hostname", alias), "user": hc.get("user", ""),
                          "port": int(hc.get("port", 22)),
                          "identityfile": (hc.get("identityfile") or [""])[0],
                          "proxy": bool(hc.get("proxycommand") or hc.get("proxyjump"))})
    keys, ppk = [], []
    if ssh_dir.is_dir():
        for f in sorted(ssh_dir.iterdir()):
            if not f.is_file() or f.name in NOT_KEYS or f.suffix in (".pub", ".old", ".bak"):
                continue
            (ppk if f.suffix == ".ppk" else keys).append(str(f))
    return {"hosts": hosts, "keys": keys, "ppk": ppk, "config_found": cfg_path.exists()}


def _is_time(name: str) -> bool:
    try:
        float(name)
        return True
    except ValueError:
        return False


def _is_dir(a) -> bool:
    return stat.S_ISDIR(a.st_mode or 0)


def _parse_kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class _ChannelPool:
    """A fixed set of SFTP channels (over one or more connections) shared by worker threads."""

    def __init__(self, clients: list[paramiko.SSHClient], per_client: int):
        self.q: queue.Queue = queue.Queue()
        self.all = []
        for client in clients:
            for _ in range(per_client):
                try:
                    c = client.open_sftp()
                except (paramiko.ChannelException, paramiko.SSHException):
                    break  # server session limit reached on this connection
                self.all.append(c)
                self.q.put(c)
        if not self.all:
            raise RuntimeError("could not open an SFTP channel (server session limit?)")
        self.size = len(self.all)

    @contextmanager
    def use(self):
        c = self.q.get()
        try:
            yield c
        finally:
            self.q.put(c)

    def close(self):
        for c in self.all:
            try:
                c.close()
            except Exception:
                pass


class RemoteSession:
    def __init__(self, cfg: RemoteConfig, password: str | None = None, passphrase: str | None = None):
        self.cfg = cfg
        ssh_cfg = paramiko.SSHConfig()
        cfg_path = Path.home() / ".ssh" / "config"
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as fh:
                ssh_cfg.parse(fh)
        self._hc = ssh_cfg.lookup(cfg.host)
        self._hostname = self._hc.get("hostname", cfg.host)
        self._port = int(cfg.port or self._hc.get("port", 22))
        self._user = cfg.username or self._hc.get("user")
        self._keys = [cfg.key_file] if cfg.key_file else self._hc.get("identityfile", None)
        # kept in memory only, to open the extra parallel connections used for mirroring
        self._password, self._passphrase = password or None, passphrase or None
        self.client = self._connect()
        self.sftp = self.client.open_sftp()
        self.lock = threading.Lock()
        self.endpoint = f"{self._user}@{self._hostname}:{self._port}"
        self._extra: list[paramiko.SSHClient] = []
        self._max_conn = CONNECTIONS  # lowered when the server refuses more parallel connections
        self._tar: bool | None = None  # GNU tar available on the server (probed once)

    def _connect(self, timeout: float = 15) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known = Path.home() / ".ssh" / "known_hosts"
        if known.exists():
            try:
                client.load_host_keys(str(known))
            except OSError:
                pass
        # Unknown hosts are refused rather than silently trusted.
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        sock = paramiko.ProxyCommand(self._hc["proxycommand"]) if "proxycommand" in self._hc else None
        client.connect(self._hostname, port=self._port, username=self._user, password=self._password,
                       key_filename=self._keys, passphrase=self._passphrase, sock=sock,
                       look_for_keys=True, allow_agent=True, timeout=timeout, banner_timeout=timeout)
        client.get_transport().set_keepalive(30)
        return client

    def _clients(self, n: int) -> list[paramiko.SSHClient]:
        """The main connection plus up to n-1 extra ones (opened once, reused, dropped if dead)."""
        n = min(n, self._max_conn)
        self._extra = [c for c in self._extra if c.get_transport() and c.get_transport().is_active()]
        while len(self._extra) < n - 1:
            try:
                time.sleep(1.0)  # do not burst new connections (fail2ban / MaxStartups)
                self._extra.append(self._connect(timeout=6))
            except Exception:
                # e.g. MaxStartups / per-user limits: remember it and carry on with fewer connections
                self._max_conn = 1 + len(self._extra)
                break
        return [self.client] + self._extra[: n - 1]

    def has_gnu_tar(self) -> bool:
        """True if `tar` on the server is GNU tar (needed for --ignore-failed-read and -T -)."""
        if getattr(self, "_tar", None) is None:
            try:
                _, out, _ = self.client.exec_command("tar --version", timeout=15)
                self._tar = "GNU tar" in out.read(4096).decode("utf-8", "replace")
            except Exception:
                self._tar = False
        return self._tar

    def channel_pool(self) -> "_ChannelPool":
        return _ChannelPool(self._clients(CONNECTIONS), WORKERS)

    def close(self):
        for c in [self.sftp, self.client, *self._extra]:
            try:
                c.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ interactive (locked)
    def home(self) -> str:
        with self.lock:
            return self.sftp.normalize(".")

    def normalize(self, path: str) -> str:
        with self.lock:
            return self.sftp.normalize(path)

    def listdir(self, path: str) -> list[dict]:
        with self.lock:
            items = self.sftp.listdir_attr(path)
        out = [{"name": a.filename, "dir": _is_dir(a), "size": a.st_size, "mtime": a.st_mtime} for a in items]
        return sorted(out, key=lambda d: (not d["dir"], d["name"]))

    # ------------------------------------------------------------------ case discovery
    def find_cases(self, parent: str, subpath: str = "auto", progress=None) -> list[dict]:
        """Case roots under parent (or parent itself, or one level deeper), with metadata and time counts."""
        parent = self.normalize(parent)
        pool = self.channel_pool()

        def listing(path):
            with pool.use() as c:
                try:
                    return {a.filename: a for a in c.listdir_attr(path)}
                except OSError:
                    return None

        def read_small(path):
            with pool.use() as c:
                with c.open(path) as fh:
                    return fh.read().decode("utf-8", "replace")

        def probe(root: str, name: str) -> dict | None:
            """None if root is not a case; else the case dict (data_dir may be None = not started)."""
            entries = listing(root)
            if entries is None:
                return None
            cands = [subpath] if subpath and subpath != "auto" else ["main", "."]
            data_dir, d_entries, top_mesh = None, None, False
            for c in cands:
                d = root if c == "." else posixpath.join(root, c)
                de = entries if c == "." else listing(d) if c in entries else None
                if de is None:
                    continue
                pm = listing(posixpath.join(d, "constant/polyMesh")) if "constant" in de else None
                if pm and "owner" in pm:
                    data_dir, d_entries, top_mesh = d, de, True
                    break
                if "processor0" in de and "owner" in (listing(posixpath.join(d, "processor0/constant/polyMesh")) or {}):
                    data_dir, d_entries = d, de
                    break
            looks_like_case = data_dir or "main" in entries or any(m in entries for m in META_FILES)
            if not looks_like_case:
                return None
            meta = {}
            for m in META_FILES:
                if m in entries and (entries[m].st_size or 0) < 65536:
                    try:
                        meta.update(_parse_kv(read_small(posixpath.join(root, m))))
                    except OSError:
                        pass
            n_times, n_proc, last, proc_only = 0, 0, None, 0
            if d_entries is not None:
                # only reconstructed (top-level) times are mirrored and analysed
                times = sorted(float(k) for k, a in d_entries.items() if _is_dir(a) and _is_time(k))
                procs = [k for k, a in d_entries.items() if _is_dir(a) and k.startswith("processor") and k[9:].isdigit()]
                n_proc = len(procs)
                if n_proc:
                    pe = listing(posixpath.join(data_dir, "processor0")) or {}
                    proc_only = len({float(k) for k, a in pe.items() if _is_dir(a) and _is_time(k)} - set(times))
                # time 0 usually holds only initial fields; it is still counted like the local scan does
                n_times, last = len(times), (max(times) if times else None)
            return {"name": name, "root": root, "data_dir": data_dir, "meta": meta, "n_times": n_times,
                    "n_processors": n_proc, "last_time": last, "n_proc_only": proc_only,
                    "reconstructed_mesh": top_mesh}

        try:
            own = probe(parent, posixpath.basename(parent.rstrip("/")) or parent)
            if own and own["data_dir"]:
                return [own]
            top = listing(parent) or {}
            subdirs = sorted(k for k, a in top.items() if _is_dir(a) and not k.startswith("."))
            found = self._probe_many([(posixpath.join(parent, d), d) for d in subdirs], probe, progress, pool.size)
            if not found:
                # one level deeper: e.g. distance_recheck_100runs/1.0A/run001_x001
                deeper = []
                for d in subdirs:
                    sub = listing(posixpath.join(parent, d)) or {}
                    deeper += [(posixpath.join(parent, d, k), f"{d}/{k}") for k, a in sorted(sub.items())
                               if _is_dir(a) and not k.startswith(".")]
                found = self._probe_many(deeper, probe, progress, pool.size)
            return found
        finally:
            pool.close()

    @staticmethod
    def _probe_many(items, probe, progress, workers) -> list[dict]:
        out = []
        if not items:
            return out
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe, root, name): i for i, (root, name) in enumerate(items)}
            done = 0
            res = [None] * len(items)
            for f in as_completed(futs):
                res[futs[f]] = f.result()
                done += 1
                if progress:
                    progress(done, len(items))
        return [r for r in res if r]

    # ------------------------------------------------------------------ mirroring
    def mirror_cases(self, cases: list[dict], local_root: Path, fields: list[str], lagrangian: bool,
                     stop=None, progress=None, log=None, time_filter=None, bulk=True) -> dict[str, Path]:
        """Download mesh, metadata and the selected fields of every case, in parallel.

        - one directory listing per directory (no per-file stat), files fetched with read-ahead;
        - files whose size and mtime match the local copy are reused;
        - a per-case manifest remembers each time directory's mtime: unchanged directories are not
          listed again (the newest two are always re-checked, they may still be being written);
        - directory listings run in parallel across cases, processors and time directories, so one
          large case (e.g. 1000 times x 16 processors) is as fast as many small ones;
        - time_filter(names) -> kept names restricts the time directories (range / thinning);
        - only reconstructed data is mirrored (top-level time directories and constant/polyMesh);
          processor*/ directories are never downloaded;
        - bulk: new time directories are not listed at all; their files are streamed with tar
          (falls back to SFTP when the server has no GNU tar).
        Returns {case name: local root}. progress(dict) receives counters for the UI.
        """
        stop = stop or (lambda: False)
        use_tar = bool(bulk) and self.has_gnu_tar()
        pool = self.channel_pool()
        st = {"phase": "list", "cases_total": len(cases), "cases_listed": 0, "dirs_total": 0, "dirs_done": 0,
              "files_total": 0, "files_done": 0, "files_reused": 0, "bytes_total": 0, "bytes_done": 0,
              "current": "", "errors": 0, "channels": pool.size, "t0": time.time(), "mode": "tar" if use_tar else "sftp"}
        st_lock = threading.Lock()
        wanted = {"fields": sorted(fields), "lagrangian": bool(lagrangian)}
        last_report = [0.0]

        def report(force=True):
            if progress and (force or time.time() - last_report[0] > 0.5):
                last_report[0] = time.time()
                progress(dict(st))

        def local_root_of(case):
            return local_root / case["root"].strip("/").replace(":", "_")

        def load_manifest(lroot: Path) -> dict:
            try:
                m = json.loads((lroot / SYNC_MANIFEST).read_text(encoding="utf-8"))
                return m if {k: m.get(k) for k in wanted} == wanted else {}
            except (OSError, ValueError):
                return {}

        def ls(path, current=None):
            with pool.use() as c:
                try:
                    out = c.listdir_attr(path)
                except OSError:
                    out = None
            with st_lock:
                st["dirs_done"] += 1
                if current:
                    st["current"] = current
            report(False)
            return out

        def files_in(ents, rdir, ldir, names, key=None) -> list:
            if ents is None:
                return []
            ents = {a.filename: a for a in ents}
            return [(posixpath.join(rdir, n), ldir / n, ents[n].st_size or 0, ents[n].st_mtime or 0, key)
                    for n in names if n in ents and not _is_dir(ents[n])]

        def parallel(fn, items, n_lists=1) -> list:
            """fn over items on the channel pool; a failing item is logged and yields None."""
            def safe(it):
                if stop():
                    return None
                try:
                    return fn(it)
                except Exception as exc:
                    with st_lock:
                        st["errors"] += 1
                    if log and st["errors"] <= 20:
                        log(f"listing failed: {exc}", "warn")
                    return None
            with st_lock:
                st["dirs_total"] += n_lists * len(items)
            if not items:
                return []
            with ThreadPoolExecutor(max_workers=pool.size) as ex:
                return list(ex.map(safe, items))

        def is_tdir(a):
            return _is_dir(a) and _is_time(a.filename)

        # 1) plan: which files to fetch, in three parallel rounds of directory listings
        plans = {cs["name"]: {"todo": [], "listed": {}, "kept": {}, "lroot": local_root_of(cs),
                              "man": load_manifest(local_root_of(cs)).get("dirs", {})} for cs in cases}

        # a) case root (metadata files) and data directory (time dirs, processors)
        def head(cs):
            pl = plans[cs["name"]]
            out = {"todo": files_in(ls(cs["root"], cs["name"]), cs["root"], pl["lroot"], META_FILES), "bases": []}
            ddir = cs.get("data_dir")
            if ddir:
                rel = posixpath.relpath(ddir, cs["root"])
                ldata = pl["lroot"] / rel if rel != "." else pl["lroot"]
                ents = ls(ddir) or []
                sdir = posixpath.join(ddir, "system")  # small settings files: thermostat targets etc.
                sys_ents = ls(sdir) or []
                out["todo"] += files_in(sys_ents, sdir, ldata / "system",
                                        [a.filename for a in sys_ents if not _is_dir(a) and (a.st_size or 0) <= SYSTEM_MAX_BYTES])
                # only reconstructed data (top-level time directories and mesh) is mirrored:
                # processor*/ directories are never downloaded
                names = {a.filename for a in ents if is_tdir(a)}
                sel = set(time_filter(sorted(names, key=float))) if time_filter else None
                out["sel"] = sel
                out["bases"] = [(ddir, ldata, "", ents)]
            return out

        heads = dict(zip([cs["name"] for cs in cases], parallel(head, cases, 3)))
        if log:
            for cs in cases:
                if cs.get("n_proc_only"):
                    log(f"[{cs['name']}] {cs['n_proc_only']} time(s) exist only in processor*/ (not reconstructed): "
                        "not downloaded — run reconstructPar on the server to include them", "warn")
        if stop():
            pool.close()
            return {}

        # b) every base (data dir and each processor): mesh, constant, and its time directories
        def base(item):
            name, (rbase, lbase, kprefix, ents), sel = item
            man = plans[name]["man"]
            mdir = posixpath.join(rbase, "constant/polyMesh")
            todo = files_in(ls(mdir, name), mdir, lbase / "constant/polyMesh", MESH_FILES)
            cdir = posixpath.join(rbase, "constant")
            todo += files_in(ls(cdir), cdir, lbase / "constant", CONST_FILES)
            if ents is None:
                ents = ls(rbase) or []
            tdirs = sorted((a for a in ents if is_tdir(a)), key=lambda a: float(a.filename))
            newest = {a.filename for a in tdirs[-2:]}
            kept, listed, dirs = {}, {}, []
            for a in tdirs:
                if sel is not None and a.filename not in sel:
                    continue
                key = f"{kprefix}{a.filename}"
                ldir = lbase / a.filename
                # synced before and still here (a field the server never wrote must not force a re-download)
                if a.filename not in newest and man.get(key) == a.st_mtime and \
                        any((ldir / f).exists() for f in fields):
                    kept[key] = a.st_mtime
                    continue
                if a.filename not in newest:  # the newest are never trusted next time
                    listed[key] = a.st_mtime
                tdir = posixpath.join(rbase, a.filename)
                if use_tar:  # no listing: ask tar for the expected files, missing ones are skipped
                    todo += [(posixpath.join(tdir, f), ldir / f, None, None, key) for f in fields]
                    if lagrangian:
                        cdir = posixpath.join(tdir, "lagrangian/moleculeCloud")
                        todo += [(posixpath.join(cdir, f), ldir / "lagrangian/moleculeCloud" / f, None, None, key)
                                 for f in LAGRANGIAN_FILES]
                    continue
                dirs.append((name, tdir, ldir, fields, key))
                if lagrangian:
                    dirs.append((name, posixpath.join(tdir, "lagrangian/moleculeCloud"),
                                 ldir / "lagrangian/moleculeCloud", LAGRANGIAN_FILES, key))
            return {"todo": todo, "kept": kept, "listed": listed, "dirs": dirs}

        bases = [(cs["name"], b, (heads.get(cs["name"]) or {}).get("sel")) for cs in cases
                 for b in (heads.get(cs["name"]) or {}).get("bases", [])]
        tdir_tasks = []
        for (name, _, _), r in zip(bases, parallel(base, bases, 3)):
            if r is None:
                continue
            pl = plans[name]
            pl["todo"] += r["todo"]
            pl["kept"].update(r["kept"])
            pl["listed"].update(r["listed"])
            tdir_tasks += r["dirs"]
        for cs in cases:
            plans[cs["name"]]["todo"] += (heads.get(cs["name"]) or {}).get("todo", [])
        if stop():
            pool.close()
            return {}

        # c) the selected time directories that changed since the last sync
        def tdir(item):
            name, rdir, ldir, names, key = item
            return name, files_in(ls(rdir, name), rdir, ldir, names, key)

        for r in parallel(tdir, tdir_tasks):
            if r is not None:
                plans[r[0]]["todo"] += r[1]
        st["cases_listed"] = len(cases)
        st["dirs_done"] = st["dirs_total"]  # estimates above (e.g. no processors) settle here
        st["files_reused"] += sum(len(pl["kept"]) for pl in plans.values()) * len(fields)
        report()
        if stop():
            pool.close()
            return {}

        # 2) skip files already mirrored with the same size+mtime
        jobs = []
        for cs in cases:
            for rpath, lpath, size, mtime, key in plans[cs["name"]]["todo"]:
                if size is not None and lpath.exists():
                    lst = lpath.stat()
                    if lst.st_size == size and int(lst.st_mtime) == int(mtime):
                        st["files_reused"] += 1
                        continue
                jobs.append((cs["name"], rpath, lpath, size, mtime, key))
        st.update(phase="download", files_total=len(jobs),
                  bytes_total=0 if use_tar else sum(j[3] or 0 for j in jobs))
        report()
        if log:
            how = "tar streams" if use_tar else f"{pool.size} parallel SFTP channels"
            size = "" if use_tar else f" ({st['bytes_total'] / 1e6:.1f} MB)"
            log(f"sync: {len(jobs)} files to download{size}, {st['files_reused']} already up to date, {how}")

        # 3) download in parallel (open + read-ahead of the known size; no extra stat round trip)
        failed: set[tuple[str, str]] = set()

        def fetch(job):
            name, rpath, lpath, size, mtime, key = job
            if stop():
                failed.add((name, key))
                return
            with pool.use() as c:
                with c.open(rpath, "rb") as fh:
                    if size:
                        fh.prefetch(size)
                    data = fh.read()
            lpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = lpath.with_name(lpath.name + f".{threading.get_ident()}.part")
            tmp.write_bytes(data)
            tmp.replace(lpath)
            os.utime(lpath, (mtime, mtime))
            with st_lock:
                st["files_done"] += 1
                st["bytes_done"] += size
                st["current"] = name
            if st["files_done"] % 25 == 0 or st["files_done"] == st["files_total"]:
                report()

        def save(lpath: Path, data: bytes, mtime) -> None:
            lpath.parent.mkdir(parents=True, exist_ok=True)
            tmp = lpath.with_name(lpath.name + f".{threading.get_ident()}.part")
            tmp.write_bytes(data)
            tmp.replace(lpath)
            os.utime(lpath, (mtime, mtime))

        def tar_fetch(item):
            """One `tar -c` stream for a chunk of one case's files (paths relative to the case root)."""
            client, root, chunk = item
            name = chunk[0][0]
            want = {posixpath.relpath(j[1], root): j for j in chunk}
            if stop():
                failed.update((name, j[5]) for j in chunk)
                return
            chan = client.get_transport().open_session()
            try:
                # no shell redirection (login shells differ: csh would mangle `2>`); stderr is drained below
                chan.exec_command(f"tar -C {shlex.quote(root)} -cf - --ignore-failed-read -T -")
                listing = ("\n".join(want) + "\n").encode("utf-8")

                def feed():
                    try:
                        chan.sendall(listing)
                        chan.shutdown_write()
                    except Exception:
                        pass
                err = bytearray()

                def drain():  # warnings about missing files must not fill the channel window
                    try:
                        while True:
                            b = chan.recv_stderr(32768)
                            if not b:
                                break
                            err.extend(b)
                            del err[:-4096]
                    except Exception:
                        pass
                threading.Thread(target=feed, daemon=True).start()
                threading.Thread(target=drain, daemon=True).start()
                with tarfile.open(fileobj=chan.makefile("rb"), mode="r|") as tf:
                    for m in tf:
                        if stop():
                            break
                        j = want.pop(m.name, None)  # only the requested paths are ever written
                        if j is None or not m.isfile():
                            continue
                        data = tf.extractfile(m).read()
                        save(j[2], data, m.mtime)
                        with st_lock:
                            st["files_done"] += 1
                            st["bytes_done"] += len(data)
                            st["current"] = name
                        report(False)
                rc = chan.recv_exit_status()
            finally:
                chan.close()
            if stop() or rc not in (0, 1, -1):
                failed.update((name, j[5]) for j in want.values())
                if rc not in (0, 1, -1):
                    tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
                    raise RuntimeError(f"tar exited with status {rc}: {tail[0]}")
            else:
                with st_lock:
                    st["files_missing"] = st.get("files_missing", 0) + len(want)  # not on the server: skipped

        if use_tar:
            clients = self._clients(CONNECTIONS)
            by_case: dict[str, list] = {}
            for j in jobs:
                by_case.setdefault(j[0], []).append(j)
            items = []
            roots = {cs["name"]: cs["root"] for cs in cases}
            for name, js in by_case.items():
                for i in range(0, len(js), TAR_CHUNK):
                    items.append((clients[len(items) % len(clients)], roots[name], js[i:i + TAR_CHUNK]))
            with ThreadPoolExecutor(max_workers=max(1, TAR_STREAMS * len(clients))) as ex:
                futs = {ex.submit(tar_fetch, it): it for it in items}
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception as exc:
                        it = futs[f]
                        failed.update((j[0], j[5]) for j in it[2])
                        st["errors"] += 1
                        if log and st["errors"] <= 20:
                            log(f"[{it[2][0][0]}] tar download failed: {exc}", "warn")
            report()
        else:
            with ThreadPoolExecutor(max_workers=pool.size) as ex:
                futs = {ex.submit(fetch, j): j for j in jobs}
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception as exc:
                        j = futs[f]
                        failed.add((j[0], j[5]))
                        st["errors"] += 1
                        if log and st["errors"] <= 20:
                            log(f"[{j[0]}] download failed: {j[1]}: {exc}", "warn")
        pool.close()

        # 4) remember verified time directories
        for cs in cases:
            pl = plans[cs["name"]]
            dirs = dict(pl["kept"])
            dirs.update({k: v for k, v in pl["listed"].items() if (cs["name"], k) not in failed})
            if dirs and not stop():
                try:
                    pl["lroot"].mkdir(parents=True, exist_ok=True)
                    (pl["lroot"] / SYNC_MANIFEST).write_text(json.dumps({**wanted, "dirs": dirs}), encoding="utf-8")
                except OSError:
                    pass
        st["phase"] = "done"
        report()
        if log:
            dt = time.time() - st["t0"]
            log(f"sync finished: {st['files_done']} downloaded ({st['bytes_done'] / 1e6:.1f} MB) in {dt:.0f}s, "
                f"{st['files_reused']} up to date, {st.get('files_missing', 0)} not on the server, {st['errors']} errors"
                f" [{st['mode']}]")
        return {cs["name"]: local_root_of(cs) for cs in cases}
