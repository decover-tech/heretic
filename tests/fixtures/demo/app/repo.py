from sqlalchemy import Column, String


class Base:
    pass


class Doc(Base):
    __tablename__ = "docs"
    org_id = Column(String(50))
    doc_id = Column(String(50))


class Plain(Base):
    __tablename__ = "plain"
    name = Column(String(50))


def get_doc(session, org_id, doc_id):
    return session.query(Doc).filter(Doc.org_id == org_id, Doc.doc_id == doc_id).first()


def list_docs(session, org_id):
    q = session.query(Doc)
    q = q.filter(Doc.org_id == org_id)
    return q.all()


def count_docs(session, org_id):
    return session.query(Doc).filter_by(org_id=org_id).count()


def delete_doc(session, org_id, doc_id):
    return session.query(Doc).filter(Doc.org_id == org_id, Doc.doc_id == doc_id).delete()


def leak_doc(session, doc_id):
    return session.query(Doc).filter(Doc.doc_id == doc_id).first()  # BUG: no org_id


def plain(session):
    return session.query(Plain).all()


def get_doc_filters(session, org_id, doc_id, include_deleted=False):
    filters = [Doc.doc_id == doc_id, Doc.org_id == org_id]
    if not include_deleted:
        filters.append(Doc.doc_id != "")
    return session.query(Doc).filter(*filters).first()


def bump_holds(session, org_id, file_id):
    hold_ids = session.query(Doc.doc_id).filter(Doc.org_id == org_id, Doc.doc_id == file_id).all()
    for (hold_id,) in hold_ids:
        session.query(Doc).filter(Doc.doc_id == hold_id).delete()


def leak_with_param(session, org_id, doc_id):
    return session.query(Doc).filter(Doc.doc_id == doc_id).first()  # BUG: org_id param ignored
