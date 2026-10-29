import { useState } from 'react';
import type { Idea } from '../types';
import './IdeaCard.css';

interface IdeaCardProps {
    idea: Idea;
    onEdit: (idea: Idea) => void;
    onDelete: (id: string) => void;
}

export function IdeaCard({ idea, onEdit, onDelete }: IdeaCardProps) {
    const [showActions, setShowActions] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

    const handleDelete = () => {
        setShowDeleteConfirm(true);
    };

    const handleConfirmDelete = () => {
        onDelete(idea.id);
        setShowDeleteConfirm(false);
    };

    const handleCancelDelete = () => {
        setShowDeleteConfirm(false);
    };

    return (
        <div
            className="idea-card"
            onMouseEnter={() => setShowActions(true)}
            onMouseLeave={() => setShowActions(false)}
        >
            <div className="idea-card-content">
                <p className="idea-text">{idea.text}</p>
                <span className="idea-date">
                    {new Date(idea.created_at).toLocaleDateString()}
                </span>
            </div>
            {showActions && (
                <div className="idea-card-actions">
                    {showDeleteConfirm ? (
                        <div className="idea-delete-confirm">
                            <span className="idea-delete-confirm-text">Delete this idea?</span>
                            <button onClick={handleCancelDelete} className="idea-cancel-btn">
                                Cancel
                            </button>
                            <button onClick={handleConfirmDelete} className="idea-confirm-delete-btn">
                                Delete
                            </button>
                        </div>
                    ) : (
                        <>
                            <button onClick={() => onEdit(idea)} className="idea-edit-btn">
                                Edit
                            </button>
                            <button onClick={handleDelete} className="idea-delete-btn">
                                Delete
                            </button>
                        </>
                    )}
                </div>
            )}
        </div>
    );
}
