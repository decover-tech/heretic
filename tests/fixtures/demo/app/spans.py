def w1(t):
    t.begin_txn()
    t.work()
    t.commit_txn()


def w2(t):
    t.begin_txn()
    t.other()
    t.commit_txn()


def w3(t):
    t.begin_txn()
    t.commit_txn()


def w4(t):
    t.begin_txn()
    t.more()
    t.commit_txn()


def w5(t):
    t.begin_txn()
    t.stuff()
    t.commit_txn()


def w6(t):
    t.begin_txn()
    t.stuff2()
    t.commit_txn()


def w7(t):
    t.begin_txn()
    t.stuff3()
    t.commit_txn()


def w_bad(t):
    t.begin_txn()  # BUG: never commits
    t.work()
