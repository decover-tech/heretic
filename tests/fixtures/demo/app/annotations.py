from typing import Optional


class User:
    name: str


def find(uid) -> User:
    if uid < 0:
        return None  # BUG: annotated non-Optional
    return User()


def maybe(uid) -> User:  # BUG: falls off end
    if uid:
        return User()


def ok(uid) -> Optional[User]:
    if uid:
        return User()
    return None


def ok_raise(uid) -> User:
    if uid:
        return User()
    raise ValueError(uid)


def ok_loop() -> int:
    while True:
        return 1


def ok_try(x) -> int:
    try:
        return int(x)
    except ValueError:
        raise
