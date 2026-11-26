USERS = {}


def lookup_user(uid):
    if uid not in USERS:
        return None
    return USERS[uid]
