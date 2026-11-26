from .lookup import lookup_user


def a(uid):
    u = lookup_user(uid)
    if u is None:
        return None
    return u.name


def b(uid):
    u = lookup_user(uid)
    if not u:
        raise KeyError(uid)
    return u.email


def c(uid):
    u = lookup_user(uid)
    if u is None:
        return ""
    return u.id


def d(uid):
    user = lookup_user(uid)
    return user.name  # BUG: 3 of 4 callers check


def e(uid):
    return lookup_user(uid).email  # BUG: direct deref


def f(uid):
    u = lookup_user(uid)
    if u is None:
        return None
    return u.org
