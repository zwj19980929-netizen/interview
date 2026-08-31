from app.transport.http.fields.core import list_of, nested, raw, string


PUBLIC_CANDIDATE_FIELDS = {
    "name": string(default="候选人"),
}

PUBLIC_TURN_FIELDS = {
    "id": string(),
    "order": raw(),
    "status": string(),
    "question_spoken_text": string(),
    "is_followup": raw(default=False),
    "parent_turn_id": raw(default=None),
    "root_turn_id": raw(default=None),
    "followup_depth": raw(default=0),
}

PUBLIC_ANSWER_FIELDS = {
    "id": raw(default=None),
    "turn_id": raw(default=None),
    "evaluation_status": raw(default=None),
}

PUBLIC_INTERVIEW_FIELDS = {
    "id": string(),
    "status": string(),
    "phase": raw(default=None),
    "avatar_mode": string(default="local"),
    "record_video": raw(default=False),
    "speech_dialogue_mode": string(default="cascade"),
    "current_turn_id": raw(default=None),
    "candidate": nested(PUBLIC_CANDIDATE_FIELDS, default={}),
    "turns": list_of(nested(PUBLIC_TURN_FIELDS), default=[]),
    "answers": list_of(nested(PUBLIC_ANSWER_FIELDS), default=[]),
    "created_at": raw(default=None),
    "updated_at": raw(default=None),
}

PUBLIC_AUDIO_ANSWER_FIELDS = {
    "status": string(),
    "next_turn_id": raw(default=None),
    "answer": nested(PUBLIC_ANSWER_FIELDS, default={}),
    "evaluation": nested({"status": string(default="completed")}, default={}),
}
