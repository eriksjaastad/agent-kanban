"""The Outreach API: contacts, who was contacted and who replied."""

from db.manager import DatabaseManager
from db.outreach import ContactStateConflictError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.common import _serialized, logger

router = APIRouter()


class OutreachContactRequest(BaseModel):
    name: str


@router.get("/api/outreach/contacts")
def list_outreach_contacts():
    try:
        contacts = DatabaseManager().list_active_contacts()
    except Exception:
        logger.exception("Failed to list outreach contacts")
        raise HTTPException(status_code=500, detail="Failed to list contacts")
    return {
        "contacts": [
            {
                "id": contact["id"],
                "name": contact["name"],
                "contacted_at": contact["contacted_at"],
                "created_at": contact["created_at"],
            }
            for contact in contacts
        ]
    }


@router.post("/api/outreach/contacts", status_code=201)
@_serialized
def add_outreach_contact(payload: OutreachContactRequest):
    try:
        contact = DatabaseManager().add_contact(payload.name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception:
        logger.exception("Failed to add outreach contact")
        raise HTTPException(status_code=500, detail="Failed to add contact")
    return {"contact": contact}


@router.patch("/api/outreach/contacts/{contact_id}")
@_serialized
def rename_outreach_contact(contact_id: int, payload: OutreachContactRequest):
    try:
        contact = DatabaseManager().rename_contact(contact_id, payload.name)
    except ContactStateConflictError:
        raise HTTPException(
            status_code=409, detail="Contact cannot be renamed after being replied"
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception:
        logger.exception("Failed to rename outreach contact %s", contact_id)
        raise HTTPException(status_code=500, detail="Failed to rename contact")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@router.post("/api/outreach/contacts/{contact_id}/contacted")
@_serialized
def mark_outreach_contacted(contact_id: int):
    try:
        contact = DatabaseManager().mark_contacted(contact_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Contact cannot be marked contacted")
    except Exception:
        logger.exception("Failed to mark outreach contact %s as contacted", contact_id)
        raise HTTPException(status_code=500, detail="Failed to mark contacted")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@router.post("/api/outreach/contacts/{contact_id}/replied")
@_serialized
def mark_outreach_replied(contact_id: int):
    try:
        contact = DatabaseManager().mark_replied(contact_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Contact cannot be marked replied")
    except Exception:
        logger.exception("Failed to mark outreach contact %s as replied", contact_id)
        raise HTTPException(status_code=500, detail="Failed to mark replied")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@router.delete("/api/outreach/contacts/{contact_id}")
@_serialized
def soft_delete_outreach_contact(contact_id: int):
    try:
        contact = DatabaseManager().soft_delete_contact(contact_id)
    except Exception:
        logger.exception("Failed to delete outreach contact %s", contact_id)
        raise HTTPException(status_code=500, detail="Failed to delete contact")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}
