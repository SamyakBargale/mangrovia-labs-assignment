import os
import sys
import shutil
import random
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add parent directory to path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import conversation
from config import settings
from agents.pricer import estimate_price, PricerResult
from agents.negotiator import write_opening_message, continue_negotiation, _get_buyer_ceiling, _get_target_ceiling, money
from conversation import ConversationState, SessionKey, Turn

app = FastAPI(title="CarBot Dashboard")

# Ensure static directory exists
os.makedirs("dashboard/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")

async def hydrate_state(session_id: int) -> ConversationState:
    """Hydrate ConversationState from SQLite for the given session ID."""
    async with db.get_connection() as conn:
        cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        sess_row = await cursor.fetchone()
        if not sess_row:
            raise HTTPException(status_code=404, detail="Session not found")
            
        estimate = None
        if sess_row["estimate_id"]:
            cursor = await conn.execute("SELECT * FROM estimates WHERE id = ?", (sess_row["estimate_id"],))
            est_row = await cursor.fetchone()
            if est_row:
                estimate = PricerResult(
                    status=est_row["status"],
                    low_price=est_row["low_price"],
                    high_price=est_row["high_price"],
                    currency=est_row["currency"],
                    reasoning=est_row["reasoning"],
                    raw_response=est_row["raw_response"]
                )
                
        cursor = await conn.execute(
            "SELECT * FROM negotiation_turns WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        turn_rows = await cursor.fetchall()
        history = []
        for t in turn_rows:
            # Skip system/summary messages in conversation history for prompt formatting
            if t["model_status"] == "summary":
                continue
            history.append(Turn(
                speaker=t["speaker"],
                text=t["text"],
                offered_price=t["offered_price"],
                currency=t["currency"]
            ))
            
        key = SessionKey(
            chat_id=sess_row["chat_id"],
            user_id=sess_row["user_id"],
            thread_id=sess_row["thread_id"]
        )
        state = ConversationState(
            key=key,
            phase=sess_row["phase"],
            description=sess_row["description"],
            estimate=estimate,
            history=history,
            session_id=sess_row["id"],
            estimate_id=sess_row["estimate_id"]
        )
        return state

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serve the main dashboard page."""
    with open("dashboard/templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/sessions")
async def get_sessions(user_id: Optional[int] = None):
    """List all negotiation sessions with estimate ranges."""
    async with db.get_connection() as conn:
        if user_id is not None:
            cursor = await conn.execute(
                """
                SELECT s.*, e.low_price, e.high_price, e.currency, e.reasoning
                FROM sessions s
                LEFT JOIN estimates e ON s.estimate_id = e.id
                WHERE s.user_id = ?
                ORDER BY s.id DESC
                """,
                (user_id,)
            )
        else:
            cursor = await conn.execute(
                """
                SELECT s.*, e.low_price, e.high_price, e.currency, e.reasoning
                FROM sessions s
                LEFT JOIN estimates e ON s.estimate_id = e.id
                ORDER BY s.id DESC
                """
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/sessions/{session_id}/turns")
async def get_session_turns(session_id: int):
    """Retrieve all turns for a specific session."""
    async with db.get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM negotiation_turns WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

@app.post("/api/sessions/new")
async def create_session(
    description: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user_id: Optional[int] = Form(None),
    chat_id: Optional[int] = Form(None)
):
    """Start a new negotiation session using a text description or photo upload."""
    if not description and not photo:
        raise HTTPException(status_code=400, detail="Description or Photo is required")

    photo_path = None
    if photo and photo.filename:
        os.makedirs("photos", exist_ok=True)
        # Create unique filename
        filename = f"web_{int(random.random()*100000)}_{photo.filename.replace(' ', '_')}"
        photo_path = os.path.join("photos", filename)
        with open(photo_path, "wb") as buffer:
            shutil.copyfileobj(photo.file, buffer)

    chat_id = chat_id or user_id or 999999
    user_id = user_id or 999999
    thread_id = None if user_id != 999999 else random.randint(100000, 999999)

    # Cancel/reset any existing active sessions for this chat/user/thread
    if user_id != 999999:
        async with db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET status = 'reset', phase = 'idle', ended_at = ?
                WHERE chat_id = ? AND user_id = ? AND status = 'active'
                """,
                (db.now_iso(), chat_id, user_id)
            )


    state = ConversationState(
        key=SessionKey(chat_id=chat_id, user_id=user_id, thread_id=thread_id),
        phase="idle",
        description=description or ""
    )

    session_id = await db.get_or_create_session(
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
        phase="idle",
        description=state.description
    )
    state.session_id = session_id

    # Estimate price
    estimate = await estimate_price(state.description, photo_path=photo_path)

    if estimate.extracted_description:
        state.description = estimate.extracted_description

    # Save details
    car_request_id = await db.save_car_request(
        telegram_message_id=0,
        chat_id=chat_id,
        user_id=user_id,
        thread_id=thread_id,
        description=state.description,
        raw_text=description,
        photo_path=photo_path
    )

    estimate_id = await db.save_estimate(
        car_request_id=car_request_id,
        status=estimate.status,
        low_price=estimate.low_price,
        high_price=estimate.high_price,
        currency=estimate.currency,
        reasoning=estimate.reasoning,
        raw_response=estimate.raw_response
    )
    state.estimate_id = estimate_id
    state.estimate = estimate

    if estimate.status == "insufficient_info":
        state.phase = "awaiting_info"
        await db.update_session(session_id, state.phase, state.description, estimate_id)
        
        question = estimate.clarifying_question or "Could you share a little more detail about the car?"
        await db.save_turn(
            session_id=session_id,
            speaker="buyer",
            text=question,
            model_status="insufficient_info"
        )
        return {"session_id": session_id, "phase": state.phase, "message": question, "estimate": estimate}

    # Generate opening message
    opening = await write_opening_message(state.description, estimate)

    await db.save_negotiation(
        estimate_id=estimate_id,
        opening_message=opening.message,
        opening_offer=opening.offer_price,
        currency=opening.currency
    )
    await db.save_turn(
        session_id=session_id,
        speaker="buyer",
        text=opening.message,
        model_status="opening",
        offered_price=opening.offer_price,
        currency=opening.currency
    )

    state.phase = "negotiating"
    await db.update_session(session_id, state.phase, state.description, estimate_id)

    return {"session_id": session_id, "phase": state.phase, "message": opening.message, "estimate": estimate}

@app.post("/api/sessions/{session_id}/clarify")
async def clarify_session(session_id: int, text: str = Form(...)):
    """Provide clarifying detail for an awaiting_info session."""
    state = await hydrate_state(session_id)
    if state.phase != "awaiting_info":
        raise HTTPException(status_code=400, detail="Session is not in awaiting_info phase")

    state.description = f"{state.description}\n{text}"

    await db.save_turn(
        session_id=session_id,
        speaker="seller",
        text=text
    )

    estimate = await estimate_price(state.description)

    car_request_id = await db.save_car_request(
        telegram_message_id=0,
        chat_id=state.key.chat_id,
        user_id=state.key.user_id,
        thread_id=state.key.thread_id,
        description=state.description,
        raw_text=text
    )

    estimate_id = await db.save_estimate(
        car_request_id=car_request_id,
        status=estimate.status,
        low_price=estimate.low_price,
        high_price=estimate.high_price,
        currency=estimate.currency,
        reasoning=estimate.reasoning,
        raw_response=estimate.raw_response
    )
    state.estimate_id = estimate_id
    state.estimate = estimate

    if estimate.status == "insufficient_info":
        state.phase = "awaiting_info"
        await db.update_session(session_id, state.phase, state.description, estimate_id)
        
        question = estimate.clarifying_question or "Could you share a little more detail about the car?"
        await db.save_turn(
            session_id=session_id,
            speaker="buyer",
            text=question,
            model_status="insufficient_info"
        )
        return {"session_id": session_id, "phase": state.phase, "message": question, "estimate": estimate}

    opening = await write_opening_message(state.description, estimate)

    await db.save_negotiation(
        estimate_id=estimate_id,
        opening_message=opening.message,
        opening_offer=opening.offer_price,
        currency=opening.currency
    )
    await db.save_turn(
        session_id=session_id,
        speaker="buyer",
        text=opening.message,
        model_status="opening",
        offered_price=opening.offer_price,
        currency=opening.currency
    )

    state.phase = "negotiating"
    await db.update_session(session_id, state.phase, state.description, estimate_id)

    return {"session_id": session_id, "phase": state.phase, "message": opening.message, "estimate": estimate}

@app.post("/api/sessions/{session_id}/message")
async def send_message(session_id: int, text: str = Form(...)):
    """Send a seller reply and get the buyer's negotiation counter-offer."""
    state = await hydrate_state(session_id)
    if state.phase != "negotiating":
        raise HTTPException(status_code=400, detail="Session is not in negotiating phase")

    turn = await continue_negotiation(state, text)

    # Save seller and buyer turns
    await db.save_turn(
        session_id=session_id,
        speaker="seller",
        text=text
    )
    await db.save_turn(
        session_id=session_id,
        speaker="buyer",
        text=turn.message,
        model_status=turn.status,
        offered_price=turn.offered_price,
        currency=turn.currency,
        raw_response=turn.raw_response
    )

    phase = state.phase
    status = "active"
    if turn.status in ("deal_agreed", "walk_away"):
        phase = "idle"
        status = turn.status

    await db.update_session(
        session_id,
        phase=phase,
        description=state.description,
        estimate_id=state.estimate_id,
        status=status
    )

    summary = None
    if turn.status in ("deal_agreed", "walk_away"):
        from agents.negotiator import deal_summary
        summary = deal_summary(state, turn)
        await db.save_turn(
            session_id=session_id,
            speaker="buyer",
            text=summary,
            model_status="summary"
        )

    return {
        "session_id": session_id,
        "phase": phase,
        "status": status,
        "message": turn.message,
        "summary": summary
    }

@app.post("/api/sessions/{session_id}/reset")
async def reset_session(session_id: int):
    """Manually cancel/reset a negotiation session."""
    async with db.get_connection() as conn:
        cursor = await conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        sess_row = await cursor.fetchone()
        if not sess_row:
            raise HTTPException(status_code=404, detail="Session not found")
            
        await db.update_session(
            session_id,
            phase="idle",
            description=sess_row["description"],
            estimate_id=sess_row["estimate_id"],
            status="reset"
        )
    return {"status": "reset"}

if __name__ == "__main__":
    import uvicorn
    # Make sure DB is initialized before starting
    import asyncio
    asyncio.run(db.init_db())
    print("Dashboard server running on http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
