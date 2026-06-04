from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import ContentPlanItem
from app.services.copywriter import generate_week_plan_topics
from app.services.post_manager import create_generated_post


class PlanItemNotFoundError(RuntimeError):
    pass


class PlanItemStatusError(RuntimeError):
    pass


def get_plan_item_or_raise(db: Session, item_id: int) -> ContentPlanItem:
    item = db.query(ContentPlanItem).filter(ContentPlanItem.id == item_id).first()
    if not item:
        raise PlanItemNotFoundError(f"Пункт контент-плана с ID {item_id} не найден.")
    return item


def list_plan_items(db: Session, limit: int = 20) -> list[ContentPlanItem]:
    return db.query(ContentPlanItem).order_by(ContentPlanItem.id.desc()).limit(limit).all()


def generate_week_plan(db: Session, platform: str = "telegram") -> list[ContentPlanItem]:
    topics = generate_week_plan_topics(platform=platform)
    today = date.today()
    items: list[ContentPlanItem] = []

    for index, topic in enumerate(topics):
        planned_day = today + timedelta(days=index)
        item = ContentPlanItem(
            planned_date=planned_day.isoformat(),
            topic=topic,
            platform=platform,
            status="planned",
            source="ai_week_plan",
        )
        db.add(item)
        items.append(item)

    db.commit()
    for item in items:
        db.refresh(item)

    return items


def create_post_from_plan_item(
    db: Session,
    item_id: int,
    with_image: bool = True,
) -> tuple[ContentPlanItem, object]:
    item = get_plan_item_or_raise(db, item_id)

    if item.created_post_id:
        raise PlanItemStatusError(
            f"По этому пункту уже создан пост #{item.created_post_id}."
        )

    post = create_generated_post(
        db=db,
        topic=item.topic,
        platform=item.platform,
        service_offer=None,
        with_image=with_image,
    )

    item.created_post_id = post.id
    item.status = "post_created"
    db.commit()
    db.refresh(item)

    return item, post
