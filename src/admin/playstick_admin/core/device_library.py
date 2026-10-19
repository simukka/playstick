"""The library as the STICK has it. Qt-free.

There used to be two answers to "what is in the library" and this module used to
merge them: the device's film table, and a prepared index on this machine that
the device had never been told about. Merging them was right while a preparation
and a sync were separate acts hours apart -- "I prepared it and it is not on the
stick" was the single most useful thing that tab could say.

It cannot happen any more. A film reaches the admin database and the stick in
the same job, and a film whose push failed is reported by the Prepare tab, which
is where the job that failed lives. So this asks the device and nothing else,
and the tab is honest about being empty when the device is not there: "what is
on the stick" is a question only the stick can answer.

WHAT IS STILL A JOIN, AND WHY IT HAS TO BE

/api/admin/library reports each film's base row and its edits SEPARATELY --
unlike /api/library, which serves the merged view a phone sees. That is
deliberate on the daemon's side and it is what lets this app tell a title
somebody chose from one the preparer derived.

It does mean the join is this module's job. Skipping it reads as a film marked
"edited" still showing the title the edit replaced, which is a convincing enough
bug to survive being looked at.
"""


def device_movies(films, local=None):
    """The device's rows, joined to their edits, in the shape the views read.

    `local` is {id: prepared row} from the admin database, for the handful of
    facts only a preparation knows -- what the encode came out as, how confident
    the TMDb match was, what the verifier complained about. The device has no
    column for any of them because none of them is anything the device does.
    """
    local = local or {}
    rows = []
    for film in films or []:
        rows.append(_join(film, local.get(film.get("id"))))
    rows.sort(key=lambda m: (m.get("sort_title") or m.get("title") or "").lower())
    return rows


def _join(film, prepared_row):
    item = {}
    if prepared_row is not None:
        # Prep facts first, so the device's own fields below win wherever both
        # have an opinion. The device is the truth about what a film is called;
        # this machine is the truth about what was done to it.
        item.update({
            "duration": prepared_row["duration"],
            "width": prepared_row["width"],
            "height": prepared_row["height"],
            "vcodec": prepared_row["vcodec"],
            "acodec": prepared_row["acodec"],
            "tmdb_score": prepared_row["tmdb_score"],
            "metadata_source": prepared_row["metadata_source"],
            "poster_source": prepared_row["poster_source"],
            "prepared_at": prepared_row["prepared_at"],
            "pushed_at": prepared_row["pushed_at"],
            "notes": prepared_row["notes"],
        })
    # A None from the device is not an opinion. Its film table has a column for
    # every one of these, and a NULL in one means it was never told -- so
    # letting it through would blank out a fact this machine actually knows,
    # which is how a Library tab ends up showing a film with no duration and no
    # codec for no reason anybody can trace.
    item.update({k: v for k, v in film.items()
                 if k != "overrides" and v is not None})
    # The edits, on top -- the same join Library._hydrate does before answering
    # a phone, so this shows what is actually on the grid.
    item.update(film.get("overrides") or {})
    item["has_override"] = bool(film.get("overrides"))
    item["device_overrides"] = dict(film.get("overrides") or {})
    item["in_admin_db"] = prepared_row is not None
    return item
