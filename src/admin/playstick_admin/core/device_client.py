"""HTTP client for the on-device daemon (src/server/playstick/http.py).

Standard library only, to match the rest of the project. Every call is bounded
by a timeout and raises DeviceError on anything other than a clean response, so
the UI can show "the stick is not answering" instead of hanging.
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class DeviceError(Exception):
    pass


class DeviceClient:
    def __init__(self, base_url, timeout=8.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout

    def _url(self, path):
        if not self.base_url:
            raise DeviceError("no device URL configured")
        return self.base_url + path

    def _request(self, path, method="GET", body=None, raw=None,
                 content_type="", timeout=None):
        """One call. `body` is JSON; `raw` is bytes sent as-is.

        The two are exclusive. `raw` exists for the poster upload, which is the
        one route on the daemon that does not speak JSON -- base64 would inflate
        every poster by a third on the link whose slowness is the whole reason
        the etag comparison exists.
        """
        url = self._url(path)
        data = None
        headers = {"Accept": "application/json"}
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or self.timeout) as resp:
                answer = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except OSError:
                pass
            raise DeviceError("%s %s: HTTP %s %s"
                              % (method, path, exc.code, detail.strip())) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DeviceError("%s %s: %s" % (method, path, exc)) from exc
        if not answer:
            return {}
        try:
            return json.loads(answer.decode("utf-8"))
        except ValueError:
            return {}

    # --- reads -------------------------------------------------------------

    def library(self):
        return self._request("/api/library")

    def status(self):
        return self._request("/api/status")

    def healthz(self):
        return self._request("/healthz")

    def thumb_url(self, film_id):
        return self._url("/api/thumb/%s" % film_id)

    # --- controls ----------------------------------------------------------

    def play(self, film_id):
        return self._request("/api/play", "POST", {"id": film_id})

    def pause(self):
        return self._request("/api/pause", "POST", {})

    def resume(self):
        return self._request("/api/resume", "POST", {})

    def stop(self):
        return self._request("/api/stop", "POST", {})

    def volume(self, delta):
        return self._request("/api/volume", "POST", {"delta": int(delta)})

    def rescan(self):
        return self._request("/api/rescan", "POST", {})

    def admin_item(self, film_id, fields):
        """Edit one film's metadata on the device. The daemon accepts only
        title, sort_title, year, rating, genres and hidden and rejects the
        rest, so this passes fields straight through.

        This is where a metadata edit LIVES now. It used to be a second,
        parallel edit to the one the Library tab wrote into the preparer's
        overrides file, with nothing keeping the two in step; sync.pull_overrides
        is what carries this one back into that file before a prep run.
        """
        return self._request("/api/admin/item", "POST",
                             {"id": film_id, "fields": fields})

    # --- the library -------------------------------------------------------

    def admin_library(self):
        """The device's film table as it is stored -- paths, overrides and
        poster etags. What a sync diffs against."""
        return self._request("/api/admin/library")

    def sync_films(self, films, reconcile=True):
        """Push the film table. `reconcile` deletes films this batch does not
        mention, which is what a full library sync means.

        The whole library goes in one request on purpose: the daemon writes it
        as one transaction, so a sync that fails halfway leaves the device with
        the library it had rather than half of a new one. The timeout is longer
        than the default for the same reason -- this is the one call that is
        not instant.
        """
        return self._request("/api/admin/films", "POST",
                             {"films": list(films),
                              "reconcile": bool(reconcile)},
                             timeout=max(self.timeout, 60.0))

    def put_thumb(self, film_id, data, content_type="image/jpeg"):
        """One poster's bytes. The daemon stores them in its database and
        serves them from there, so this is what makes a poster survive a
        re-provision."""
        return self._request("/api/admin/thumb/%s" % film_id, "PUT",
                             raw=data, content_type=content_type,
                             timeout=max(self.timeout, 30.0))

    def thumb_bytes(self, film_id):
        """One poster's bytes off the device, or b"" if it has none.

        Deliberately NOT through _request, which parses JSON: this is the one
        response on the daemon that is an image. A film with no poster answers
        with the placeholder SVG rather than a 404 -- the grid never waits --
        and an SVG is not a poster, so it is filtered here rather than being
        cached and drawn as a grey letter.
        """
        req = urllib.request.Request(self._url("/api/thumb/%s" % film_id),
                                     headers={"Accept": "image/jpeg, image/png"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                kind = (resp.headers.get("Content-Type") or "").lower()
                if "svg" in kind:
                    return b""
                return resp.read()
        except urllib.error.HTTPError:
            return b""
        except (urllib.error.URLError, OSError) as exc:
            raise DeviceError("GET /api/thumb/%s: %s" % (film_id, exc)) from exc

    def clear_thumb(self, film_id):
        """Forget a poster, so the device extracts a frame instead. The way to
        undo a wrong one: a sync deliberately leaves posters alone, so nothing
        else would replace it."""
        return self._request("/api/admin/thumb/%s/clear" % film_id, "POST", {})
