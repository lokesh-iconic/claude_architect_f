"""Typed view of one extraction, parsed from the tool's input."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import ValidationIssue
from .schema import HEADER_FIELDS


@dataclass
class FieldValue:
    value: Any = None
    evidence: str | None = None
    confidence: float = 0.0


@dataclass
class LineItem:
    description: str
    quantity: float | None
    unit_price: float | None
    amount: float
    category: str
    category_detail: str | None
    evidence: str
    confidence: float


@dataclass
class Extraction:
    document_type: str
    document_type_detail: str | None
    fields: dict[str, FieldValue]
    line_items: list[LineItem]

    @classmethod
    def from_tool_input(cls, data: dict[str, Any]) -> "Extraction":
        """Raises KeyError/TypeError/ValueError on a shape mismatch."""
        fields = {}
        for name in HEADER_FIELDS:
            raw = data[name]
            fields[name] = FieldValue(raw["value"], raw["evidence"], float(raw["confidence"]))
        items = [
            LineItem(
                description=str(li["description"]),
                quantity=None if li["quantity"] is None else float(li["quantity"]),
                unit_price=None if li["unit_price"] is None else float(li["unit_price"]),
                amount=float(li["amount"]),
                category=str(li["category"]),
                category_detail=li["category_detail"],
                evidence=str(li["evidence"]),
                confidence=float(li["confidence"]),
            )
            for li in data["line_items"]
        ]
        return cls(data["document_type"], data["document_type_detail"], fields, items)

    def to_tool_input(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "document_type": self.document_type,
            "document_type_detail": self.document_type_detail,
        }
        for name, fv in self.fields.items():
            out[name] = asdict(fv)
        out["line_items"] = [asdict(li) for li in self.line_items]
        return out

    def value(self, name: str) -> Any:
        return self.fields[name].value


@dataclass
class Document:
    doc_id: str
    text: str
    source: str = ""

    @classmethod
    def from_path(cls, path: Path) -> "Document":
        return cls(path.stem, path.read_text(encoding="utf-8"), str(path.name))


@dataclass
class AttemptRecord:
    attempt: int
    stop_reason: str
    issues: list[ValidationIssue]
    extraction: dict[str, Any] | None
    feedback_sent: bool = False


@dataclass
class DocumentResult:
    doc_id: str
    status: str  # "accepted" | "review"
    extraction: Extraction | None
    attempts: list[AttemptRecord]
    review_reasons: list[str] = field(default_factory=list)
    low_confidence: list[tuple[str, float]] = field(default_factory=list)
    unresolved: list[ValidationIssue] = field(default_factory=list)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)
