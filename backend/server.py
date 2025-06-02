from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime
from emergentintegrations.llm.chat import LlmChat, UserMessage

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Define Models
class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    message: str
    sender: str  # "user" or "assistant"
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class ChatMessageCreate(BaseModel):
    session_id: str
    message: str

class ChatResponse(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    user_message: str
    assistant_message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.dict()
    status_obj = StatusCheck(**status_dict)
    _ = await db.status_checks.insert_one(status_obj.dict())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find().to_list(1000)
    return [StatusCheck(**status_check) for status_check in status_checks]

@api_router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(chat_input: ChatMessageCreate):
    try:
        # Store user message
        user_message_obj = ChatMessage(
            session_id=chat_input.session_id,
            message=chat_input.message,
            sender="user"
        )
        await db.chat_messages.insert_one(user_message_obj.dict())
        
        # Get conversation history for context
        history = await db.chat_messages.find(
            {"session_id": chat_input.session_id}
        ).sort("timestamp", 1).to_list(50)  # Last 50 messages
        
        # Initialize LLM chat with personalized system message
        system_message = """You are a friendly and helpful AI assistant. You are personal, warm, and engaging in your responses. 
        You maintain context from our conversation and provide thoughtful, personalized replies. 
        You can help with various tasks, answer questions, and have meaningful conversations."""
        
        chat = LlmChat(
            api_key=os.environ['OPENAI_API_KEY'],
            session_id=chat_input.session_id,
            system_message=system_message
        ).with_model("openai", "gpt-4o-mini").with_max_tokens(2048)
        
        # Create user message for the LLM
        user_message = UserMessage(text=chat_input.message)
        
        # Get AI response
        ai_response = await chat.send_message(user_message)
        
        # Store assistant message
        assistant_message_obj = ChatMessage(
            session_id=chat_input.session_id,
            message=ai_response,
            sender="assistant"
        )
        await db.chat_messages.insert_one(assistant_message_obj.dict())
        
        # Return response
        return ChatResponse(
            session_id=chat_input.session_id,
            user_message=chat_input.message,
            assistant_message=ai_response
        )
        
    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")

@api_router.get("/chat/history/{session_id}")
async def get_chat_history(session_id: str):
    try:
        messages = await db.chat_messages.find(
            {"session_id": session_id}
        ).sort("timestamp", 1).to_list(100)
        
        return {
            "session_id": session_id,
            "messages": [
                {
                    "id": msg["id"],
                    "message": msg["message"],
                    "sender": msg["sender"],
                    "timestamp": msg["timestamp"]
                }
                for msg in messages
            ]
        }
    except Exception as e:
        logger.error(f"History retrieval error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"History retrieval error: {str(e)}")

@api_router.post("/chat/new-session")
async def create_new_session():
    session_id = str(uuid.uuid4())
    return {"session_id": session_id}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
