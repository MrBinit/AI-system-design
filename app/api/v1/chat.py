from fastapi import APIRouter
from app.schemas.chat_schema import ChatRequest
from app.services.llm_service import generate_response

router = APIRouter()


@router.post("/chat")
async def chat(request: ChatRequest):
    result = await generate_response(request.user_id, request.prompt)
    return {"response": result}
