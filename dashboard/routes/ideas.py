"""The Ideas API: capture, edit and drop ideas."""

from db.manager import DatabaseManager
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from dashboard.common import _serialized, _stringify_pt_ids, logger

router = APIRouter()


class IdeaCreateRequest(BaseModel):
    text: str


class IdeaUpdateRequest(BaseModel):
    text: str


@router.get("/api/ideas")
def get_ideas():
    """Get all ideas.
    
    Returns:
        List of all ideas ordered by creation date (newest first)
    """
    try:
        db = DatabaseManager()
        ideas = db.get_all_ideas()
        return {"ideas": [_stringify_pt_ids(i) for i in ideas]}
    except Exception as e:
        logger.error(f"Error fetching ideas: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch ideas"
        )


@router.post("/api/ideas", status_code=status.HTTP_201_CREATED)
@_serialized
def create_idea(idea_data: IdeaCreateRequest):
    """Create a new idea.
    
    Args:
        idea_data: Idea creation data (text)
        
    Returns:
        Created idea with id and timestamps
        
    Raises:
        HTTPException: 400 if validation fails
    """
    try:
        db = DatabaseManager()
        idea = db.add_idea(idea_data.text)
        return _stringify_pt_ids(idea)
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error creating idea: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create idea"
        )


@router.patch("/api/ideas/{idea_id}")
@_serialized
def update_idea(idea_id: int, idea_data: IdeaUpdateRequest):
    """Update an idea's text.
    
    Args:
        idea_id: Idea ID
        idea_data: Updated text
        
    Returns:
        Updated idea
        
    Raises:
        HTTPException: 400 if validation fails, 404 if idea not found
    """
    try:
        db = DatabaseManager()
        idea = db.update_idea(idea_id, idea_data.text)
        return _stringify_pt_ids(idea)
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error updating idea {idea_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update idea"
        )


@router.delete("/api/ideas/{idea_id}", status_code=status.HTTP_200_OK)
@_serialized
def delete_idea(idea_id: int):
    """Delete an idea.
    
    Args:
        idea_id: Idea ID
        
    Returns:
        Success confirmation
        
    Raises:
        HTTPException: 404 if idea not found
    """
    try:
        db = DatabaseManager()
        db.delete_idea(idea_id)
        return {
            "success": True,
            "message": f"Idea {idea_id} deleted successfully"
        }
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error deleting idea {idea_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete idea"
        )
