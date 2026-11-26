def use_then_check(user):
    name = user.name
    if user is None:  # BUG: use-then-check
        return None
    return name


def check_then_use(cfg):
    if cfg is None:
        print(cfg.path)  # BUG: check-then-use
    return 1


def ok_guarded(cfg):
    if cfg is None:
        return 0
    return cfg.value


def ok_reassigned(cfg):
    if cfg is None:
        cfg = load()
    return cfg.value


def ok_short_circuit(x):
    return x is not None and x.ready


def ok_ifexp(x):
    return x.ready if x is not None else False


def ok_attr_reset(self_):
    if self_.client is None:
        self_.connect()
    return self_.client.get()


def ok_loop(node):
    while node is not None:
        node = node.parent
    return node


def ok_truthy(items):
    if not items:
        items.append(1)  # falsy != None; not a bug
    return items


def load():
    return object()


def ok_running_best(items):
    best = None
    for it in items:
        if best is None or it.score > best.score:
            best = it
    return best


def ok_poll(proc):
    while True:
        if proc.handle is None or proc.handle.poll() is not None:
            proc.restart()


def ok_retest(pending):
    if pending is not None:
        pending.add_done_callback(print)
    if pending is not None and not pending.done():
        pending.cancel()


def ok_flag(lengths, enabled):
    server_len = lengths is not None and enabled
    if server_len:
        return lengths[0]
    return None


def ok_merge(fetch):
    try:
        result = fetch()
    except Exception:
        result = None
        detail = "down"
    else:
        detail = result.sentence()
    if result is None:
        return detail
    return result


def bad_move(session, pid):
    parent = session.find(pid)
    path = parent.drive_path
    if parent is None:  # BUG: dead check, deref above raises first
        raise LookupError(pid)
    return path


def _refuse(code):
    raise PermissionError(code)


def ok_noreturn_helper(entry):
    if entry is None:
        _refuse(403)
    return entry.write


def ok_nonlocal(groups):
    frame = None

    def new_frame():
        nonlocal frame
        frame = object()
        return frame

    for g in groups:
        if frame is None:
            new_frame()
        frame.add(g)
