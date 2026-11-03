"""A real browser as a Python oracle: `probe.cjs serve` behind a context manager.

    from browser_oracle.client import BrowserOracle
    with BrowserOracle("chrome") as chrome:
        trees = chrome.trees(["<table><tr><td>x</table>", ...])
        chrome.build                      # "Chrome/156.0.8078.80"

Each tree is `__ser`'s lossless JSON serialisation of the document the browser built (see probe.cjs):
text is a string, a comment `["#c", data]`, a doctype `["#dt", name, public, system]`, the document
`["#d", children]`, an element `[localName, namespace (0 for HTML), [name, value, ...], children]`, with
an HTML template's content appended to its children as a `["#d", ...]` and an open shadow root as
`["#shadow", mode, children]`. `tree.document_view` drops those two to get what a selector can reach.

A document that contains neither `noscript` nor `shadowrootmode` is parsed by `DOMParser`; anything else
is served with `Content-Security-Policy: script-src 'none'` and page-loaded. `paths` counts which path
each document took. A null tree is an oracle error: it is recorded in `errors`, never dropped silently.

The browser binary comes from `FROSTWORK_CHROME` / `FROSTWORK_FIREFOX`, else the macOS install location;
Node from `FROSTWORK_NODE`, else `node` on PATH.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from collections import Counter
from typing import Any, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "probe.cjs")

DEFAULT_BINARIES = {
    "chrome": ("FROSTWORK_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "firefox": ("FROSTWORK_FIREFOX", "/Applications/Firefox.app/Contents/MacOS/firefox"),
}


class OracleError(RuntimeError):
    """The browser process failed, or answered something other than the request it was sent."""


def browser_binary(browser: str) -> str:
    env, default = DEFAULT_BINARIES[browser]
    return os.environ.get(env) or default


class BrowserOracle:
    """One headless browser process, kept running for the life of the `with` block.

    `trees(docs)` sends the documents in batches of `batch` and returns one tree per document, in order.
    With `selfcheck=True` every eligible document is also page-loaded, and any whose two trees differ is
    appended to `selfcheck_differ` as the document itself, so a run can fail on it.
    """

    def __init__(self, browser: str = "chrome", binary: Optional[str] = None, *, tabs: int = 8,
                 batch: int = 1000, node: Optional[str] = None):
        if browser not in DEFAULT_BINARIES:
            raise ValueError(f"browser must be one of {sorted(DEFAULT_BINARIES)}, not {browser!r}")
        self.browser = browser
        self.binary = binary or browser_binary(browser)
        self.node = node or os.environ.get("FROSTWORK_NODE") or "node"
        self.tabs = tabs
        self.batch = batch
        self.build: Optional[str] = None
        self.paths: Counter = Counter()
        self.errors: list[dict[str, Any]] = []
        self.selfcheck_checked = 0
        self.selfcheck_differ: list[str] = []
        self._proc: Optional[subprocess.Popen] = None
        self._browser_pid: Optional[int] = None
        self._next_id = 0

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> "BrowserOracle":
        if not os.path.exists(self.binary):
            raise OracleError(f"{self.browser} binary not found at {self.binary!r} "
                              f"(set {DEFAULT_BINARIES[self.browser][0]})")
        # Its own session, so a kill reaches node; node's SIGTERM handler kills the browser's own
        # process group and deletes its profile.
        self._proc = subprocess.Popen(
            [self.node, PROBE, self.browser, self.binary, "serve", str(self.tabs)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            start_new_session=True)
        try:
            ready = self._read()
        except BaseException:
            self.close()
            raise
        if not ready.get("ready"):
            self.close()
            raise OracleError(f"probe did not start: {ready}")
        self.build = ready["browser"]
        self._browser_pid = ready.get("pid")
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """End stdin so probe.cjs closes the browser itself, then escalate if it does not exit.

        SIGTERM lets node kill the browser and delete its profile. Only if node survives that is it
        killed outright, and then the browser's own process group is killed too, since it outlives node.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=15)
        except (subprocess.TimeoutExpired, BrokenPipeError, OSError):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                for pgid in (proc.pid, self._browser_pid):
                    try:
                        if pgid:
                            os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait(timeout=5)
        finally:
            if proc.stdout:
                proc.stdout.close()

    # ------------------------------------------------------------------ requests
    def _read(self) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        line = self._proc.stdout.readline()
        if not line:
            code = self._proc.poll()
            raise OracleError(f"probe.cjs exited (status {code}) without answering")
        return json.loads(line)

    def _request(self, docs: list[str], selfcheck: bool) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise OracleError("BrowserOracle is not running; use it as a context manager")
        self._next_id += 1
        rid = self._next_id
        self._proc.stdin.write(json.dumps({"id": rid, "docs": docs, "selfcheck": selfcheck}) + "\n")
        self._proc.stdin.flush()
        reply = self._read()
        if reply.get("id") != rid or "error" in reply:
            raise OracleError(f"request {rid}: {reply.get('error', reply)}")
        if len(reply["trees"]) != len(docs):
            raise OracleError(f"request {rid}: {len(reply['trees'])} trees for {len(docs)} documents")
        return reply

    def trees(self, docs: Iterable[str], *, selfcheck: bool = False) -> list[Any]:
        docs = list(docs)
        out: list[Any] = []
        for start in range(0, len(docs), self.batch):
            chunk = docs[start:start + self.batch]
            reply = self._request(chunk, selfcheck)
            self.paths.update(reply["paths"])
            for err in reply["errors"]:
                self.errors.append({"doc": chunk[err["index"]], "error": err["error"]})
            if selfcheck:
                check = reply["selfcheck"]
                self.selfcheck_checked += check["checked"]
                self.selfcheck_differ.extend(chunk[i] for i in check["differ"])
                for i in check["failed"]:
                    self.errors.append({"doc": chunk[i], "error": "self-check page load failed"})
            out.extend(reply["trees"])
        return out
