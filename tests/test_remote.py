"""Remote scan and mirroring against a fake SFTP server backed by a local folder (no network)."""

import os
import stat
import time

import numpy as np

from mdfoam_droplet_viewer import remote
from mdfoam_droplet_viewer.case import CaseData, discover_cases

from test_core import build_case


class _Attr:
    def __init__(self, path):
        s = os.stat(path)
        self.filename = os.path.basename(path)
        self.st_mode, self.st_size, self.st_mtime = s.st_mode, s.st_size, int(s.st_mtime)


class _File:
    def __init__(self, path):
        self.fh = open(path, "rb")

    def prefetch(self, size):
        pass

    def read(self):
        return self.fh.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fh.close()


class FakeSFTP:
    """Maps remote POSIX paths '/srv/...' onto a local base folder; counts calls."""

    def __init__(self, base, counter):
        self.base, self.counter = base, counter

    def _p(self, path):
        return os.path.join(self.base, path.lstrip("/"))

    def listdir_attr(self, path):
        self.counter["list"] += 1
        p = self._p(path)
        if not os.path.isdir(p):
            raise OSError(2, "No such file")
        return [_Attr(os.path.join(p, n)) for n in os.listdir(p)]

    def open(self, path, mode="r"):
        self.counter["open"] += 1
        p = self._p(path)
        if not os.path.isfile(p):
            raise OSError(2, "No such file")
        return _File(p)

    def normalize(self, path):
        return "/" + path.strip("/")

    def close(self):
        pass


class FakeClient:
    def __init__(self, base, counter):
        self.base, self.counter = base, counter

    def open_sftp(self):
        return FakeSFTP(self.base, self.counter)


def fake_session(base):
    counter = {"list": 0, "open": 0}
    s = remote.RemoteSession.__new__(remote.RemoteSession)
    s.client = FakeClient(base, counter)
    s.sftp = s.client.open_sftp()
    import threading
    s.lock = threading.Lock()
    s.cfg = remote.RemoteConfig("fake")
    s._extra, s._max_conn = [], 1
    s._clients = lambda n: [s.client]
    return s, counter


def make_remote_tree(base):
    root = base / "srv" / "runs"
    for name in ("run001_x001", "run002_x002"):
        for t in ("1e-10", "2e-10", "3e-10", "4e-10"):
            build_case(root / "1.0A" / name / "main", np.full(64, 600.0), np.full(64, 2e28), np.full(64, 300.0), time=t)
        (root / "1.0A" / name / "batch_status.txt").write_text("status=SUCCESS\n")
    (root / "1.0A" / "run003_x003" / "main").mkdir(parents=True)  # not started
    (root / "1.0A" / "run003_x003" / "case_info.txt").write_text("status=FAILED\n")
    return root


def test_find_cases_one_level_deeper_with_meta(tmp_path):
    make_remote_tree(tmp_path)
    s, _ = fake_session(str(tmp_path))
    cs = s.find_cases("/srv/runs")
    assert [c["name"] for c in cs] == ["1.0A/run001_x001", "1.0A/run002_x002", "1.0A/run003_x003"]
    assert cs[0]["n_times"] == 4 and cs[0]["meta"]["status"] == "SUCCESS" and cs[0]["data_dir"].endswith("/main")
    assert cs[2]["data_dir"] is None and cs[2]["meta"]["status"] == "FAILED"


def test_mirror_then_incremental(tmp_path):
    src = make_remote_tree(tmp_path / "server")
    s, counter = fake_session(str(tmp_path / "server"))
    cases = [c for c in s.find_cases("/srv/runs/1.0A") if c["data_dir"]]
    local = tmp_path / "mirror"
    fields = ["rhoM_water", "rhoN_water", "temperature_water"]
    prog = []
    roots = s.mirror_cases(cases, local, fields, False, progress=prog.append)
    assert prog[-1]["phase"] == "done" and prog[-1]["errors"] == 0
    assert prog[-1]["files_done"] == prog[-1]["files_total"] > 0
    # the mirror is a readable case with every time step
    f = discover_cases(roots["run001_x001"])
    c = CaseData.open("run001_x001", f[0][1], f[0][2])
    assert [t.name for t in c.times] == ["1e-10", "2e-10", "3e-10", "4e-10"]
    assert c.load_mesh().n_cells == 64

    # second sync: nothing downloaded; old time dirs are not even listed (manifest), newest two are
    counter.update(list=0, open=0)
    prog.clear()
    s.mirror_cases(cases, local, fields, False, progress=prog.append)
    assert prog[-1]["files_total"] == 0 and counter["open"] == 0
    per_case_lists = counter["list"] / len(cases)
    assert per_case_lists <= 2 + 1 + 2 + 2  # root, data dir, system/, mesh/constant + 2 newest time dirs

    # a running case writes a new time step: only that is fetched
    time.sleep(1.1)
    build_case(src / "1.0A" / "run001_x001" / "main", np.full(64, 600.0), np.full(64, 2e28), np.full(64, 300.0), time="5e-10")
    prog.clear()
    s.mirror_cases(cases, local, fields, False, progress=prog.append)
    assert prog[-1]["files_done"] >= 3  # the three fields of 5e-10 (+ mesh rewritten by build_case)
    assert (local / "srv/runs/1.0A/run001_x001/main/5e-10/rhoM_water").exists()


def test_mirror_time_filter_keeps_range_stride_and_newest(tmp_path):
    from mdfoam_droplet_viewer.settings import select_times
    make_remote_tree(tmp_path / "server")
    s, _ = fake_session(str(tmp_path / "server"))
    cases = [c for c in s.find_cases("/srv/runs/1.0A") if c["name"] == "run001_x001"]
    prog = []

    def every_other(names):
        return [names[i] for i in sorted(select_times([float(n) for n in names], 2))]

    roots = s.mirror_cases(cases, tmp_path / "mirror", ["rhoM_water"], False, progress=prog.append, time_filter=every_other)
    main = roots["run001_x001"] / "main"
    assert sorted(p.name for p in main.iterdir() if p.name[0].isdigit()) == ["1e-10", "3e-10", "4e-10"]
    assert prog[-1]["dirs_done"] == prog[-1]["dirs_total"] > 0 and prog[-1]["errors"] == 0


class _TarChannel:
    """Runs `tar -C root -cf - -T -` in-process: the paths arrive on stdin, a tar stream comes back."""

    def __init__(self, base):
        import threading
        self.base, self.buf, self.ready, self.out = base, b"", threading.Event(), None

    def exec_command(self, cmd):
        import shlex
        args = shlex.split(cmd)
        self.root = args[args.index("-C") + 1]

    def sendall(self, data):
        self.buf += data

    def shutdown_write(self):
        import io
        import tarfile
        bio = io.BytesIO()
        with tarfile.open(fileobj=bio, mode="w") as tf:
            for rel in filter(None, self.buf.decode().split("\n")):
                p = os.path.join(self.base, self.root.lstrip("/"), rel)
                if os.path.isfile(p):  # --ignore-failed-read: missing files are skipped
                    tf.add(p, arcname=rel)
        bio.seek(0)
        self.out = bio
        self.ready.set()

    def makefile(self, mode):
        chan = self

        class _R:
            def read(self, n=-1):
                chan.ready.wait(10)
                return chan.out.read(n)
        return _R()

    def recv_exit_status(self):
        return 0

    def recv_stderr(self, n):
        return b""

    def close(self):
        pass


class TarClient(FakeClient):
    def exec_command(self, cmd, timeout=None):
        import io
        return None, io.BytesIO(b"tar (GNU tar) 1.34\n"), None

    def get_transport(self):
        base = self.base

        class _T:
            def open_session(self):
                return _TarChannel(base)
        return _T()


def test_mirror_with_tar_streams_without_listing_time_dirs(tmp_path):
    make_remote_tree(tmp_path / "server")
    s, counter = fake_session(str(tmp_path / "server"))
    s.client = TarClient(str(tmp_path / "server"), counter)
    s._clients = lambda n: [s.client]
    cases = [c for c in s.find_cases("/srv/runs/1.0A") if c["data_dir"]]
    counter.update(list=0, open=0)
    prog, logs = [], []
    fields = ["rhoM_water", "rhoN_water", "temperature_water", "notAField"]
    roots = s.mirror_cases(cases, tmp_path / "mirror", fields, False, progress=prog.append,
                           log=lambda m, *a: logs.append(m))
    assert prog[-1]["mode"] == "tar" and prog[-1]["errors"] == 0
    assert counter["open"] == 0                     # nothing fetched file by file
    assert counter["list"] <= len(cases) * 5        # root, data dir, mesh, constant, (base) - no time dirs
    f = discover_cases(roots["run001_x001"])
    c = CaseData.open("run001_x001", f[0][1], f[0][2])
    assert [t.name for t in c.times] == ["1e-10", "2e-10", "3e-10", "4e-10"] and c.load_mesh().n_cells == 64
    assert prog[-1]["files_missing"] == 2 * 4       # notAField in every time dir: skipped, not an error
    # next sync: old time dirs come from the manifest, only the newest two are streamed again
    prog.clear()
    s.mirror_cases(cases, tmp_path / "mirror", fields, False, progress=prog.append)
    assert prog[-1]["files_done"] == 2 * 2 * 3


def test_processor_dirs_are_never_downloaded(tmp_path):
    src = make_remote_tree(tmp_path / "server")
    main = src / "1.0A" / "run001_x001" / "main"
    for p in ("processor0", "processor1"):
        (main / p / "constant" / "polyMesh").mkdir(parents=True)
        (main / p / "constant" / "polyMesh" / "owner").write_text("x")
        for t in ("1e-10", "2e-10", "3e-10", "4e-10", "5e-10"):  # 5e-10 is not reconstructed yet
            (main / p / t).mkdir()
            (main / p / t / "rhoM_water").write_text("x")
    for client in ("sftp", "tar"):
        s, counter = fake_session(str(tmp_path / "server"))
        if client == "tar":
            s.client = TarClient(str(tmp_path / "server"), counter)
            s._clients = lambda n: [s.client]
        cs = [c for c in s.find_cases("/srv/runs/1.0A") if c["name"] == "run001_x001"]
        assert cs[0]["n_times"] == 4 and cs[0]["n_proc_only"] == 1 and cs[0]["reconstructed_mesh"]
        logs = []
        roots = s.mirror_cases(cs, tmp_path / f"mirror_{client}", ["rhoM_water"], False,
                               log=lambda m, *a: logs.append(m))
        local = roots["run001_x001"] / "main"
        assert not any(p.name.startswith("processor") for p in local.iterdir())
        assert sorted(p.name for p in local.iterdir() if p.name[0].isdigit()) == ["1e-10", "2e-10", "3e-10", "4e-10"]
        assert any("only in processor" in m for m in logs)


def test_system_settings_are_mirrored_and_read(tmp_path):
    from mdfoam_droplet_viewer import sim
    src = make_remote_tree(tmp_path / "server")
    sysd = src / "1.0A" / "run001_x001" / "main" / "system"
    sysd.mkdir()
    (sysd / "tVEquilibrationDict").write_text(
        "FoamFile { version 2.0; format ascii; class dictionary; object x; }\n"
        "count 1;\ncoeff0 { fixT 0; T 350; }\ncoeff1 { fixT 1; fixV 1; T 300; V (0 0 0); }\n")
    (sysd / "controlDict").write_text("FoamFile { format ascii; }\napplication mdTVEquilibrationFoam;\ndeltaT 5e-15;\n")
    (sysd / "big.bin").write_bytes(b"x" * (70 * 1024))  # too large: not mirrored
    s, _ = fake_session(str(tmp_path / "server"))
    cases = [c for c in s.find_cases("/srv/runs/1.0A") if c["name"] == "run001_x001"]
    roots = s.mirror_cases(cases, tmp_path / "mirror", ["rhoM_water"], False)
    lsys = roots["run001_x001"] / "main" / "system"
    assert sorted(p.name for p in lsys.iterdir()) == ["controlDict", "tVEquilibrationDict"]
    r = sim.read_sim(lsys.parent)
    assert r["target_T"] == [300.0] and r["control"]["application"] == "mdTVEquilibrationFoam"
    assert [x["fixT"] for x in r["thermostat"]] == [False, True]
    assert ("coeff1/V", "( 0 0 0 )") in r["files"]["tVEquilibrationDict"]["rows"]
