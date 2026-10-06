"""What the ORM seeding block must satisfy, discovered rather than guessed.

Prints the NOT NULL columns (and any enum/foreign-key defaults) for the child
models the remaining 46 uncontracted routes depend on, so `seed_via_orm` can
insert valid rows first time instead of failing one IntegrityError at a time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import inspect  # noqa: E402

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401

TARGETS = [
    "EditorialPlanItem",
    "MediaAsset",
    "PublishedPost",
    "SocialInteraction",
    "SocialAccount",
    "Conversation",
    "CommunityAction",
    "Experiment",
    "Opportunity",
    "LocalizedContent",
    "UgcProjectRow",
    "LipSyncJob",
]

for name in TARGETS:
    model = getattr(Base, name, None) or getattr(__import__("app.models", fromlist=[name]), name, None)
    if model is None:
        print(f"\n{name}: NOT FOUND")
        continue
    table = model.__table__
    required = [c.name for c in table.columns if not c.nullable and c.default is None]
    defaulted = [c.name for c in table.columns if not c.nullable and c.default is not None]
    enums = {c.name: str(c.type) for c in table.columns if "Enum" in str(c.type)}
    print(f"\n{name}  (table {table.name})")
    print(f"  required-no-default : {required}")
    print(f"  required-defaulted  : {defaulted}")
    if enums:
        print(f"  enums               : {enums}")