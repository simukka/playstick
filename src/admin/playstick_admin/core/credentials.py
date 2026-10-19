"""The NAS password, kept in the desktop's keyring. Qt-free.

The keyring rather than the settings file because this is the one secret the
app holds, and the settings file is plain text anybody with a copy of the home
directory can read. The cost is that a terminal run of actl cannot have it --
see site_vars.py for how that is kept from doing harm.

The keyring is GNOME Keyring or KWallet through python3-keyring. A desktop
without one -- a bare window manager, a session over ssh -- gets a sentence
saying so rather than a traceback, and everything but the NAS login works.
"""

SERVICE = "playstick"
NAS_PASSWORD = "nas-password"


class CredentialError(Exception):
    pass


def _keyring():
    try:
        import keyring
        import keyring.errors
    except ImportError:
        raise CredentialError("python3-keyring is not installed, so the NAS "
                              "password cannot be stored.") from None
    return keyring


def get_nas_password():
    """The password, or "" when none is stored or there is no keyring."""
    try:
        keyring = _keyring()
        return keyring.get_password(SERVICE, NAS_PASSWORD) or ""
    except CredentialError:
        return ""
    except Exception:  # keyring.errors.KeyringError and backend failures
        return ""


def set_nas_password(password):
    keyring = _keyring()
    try:
        if password:
            keyring.set_password(SERVICE, NAS_PASSWORD, password)
        else:
            try:
                keyring.delete_password(SERVICE, NAS_PASSWORD)
            except keyring.errors.PasswordDeleteError:
                pass
    except keyring.errors.KeyringError as exc:
        raise CredentialError("The desktop keyring would not store the NAS "
                              "password: %s" % exc) from exc
