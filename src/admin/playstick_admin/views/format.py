"""Small shared formatting helpers for the views."""


def human_size(num):
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%.0f %s" % (size, unit) if unit in ("B", "KB") \
                else "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TB" % size


def genres_str(movie):
    genres = movie.get("genres") or []
    if isinstance(genres, str):
        return genres
    return ", ".join(genres)


def match_str(movie):
    if movie.get("override_tmdb_id"):
        return "pinned #%s" % movie["override_tmdb_id"]
    if movie.get("tmdb_id"):
        score = movie.get("tmdb_score")
        return "#%s (%s)" % (movie["tmdb_id"], score) if score else "#%s" % movie["tmdb_id"]
    return ""
