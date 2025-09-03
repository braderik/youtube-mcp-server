#!/usr/bin/env python3

import asyncio
import logging
import os
import re
from typing import Any, Dict
from urllib.parse import urlparse, parse_qs
import json

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from dotenv import load_dotenv
import aiohttp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# YouTube API constants and configuration
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
if not YOUTUBE_API_KEY:
    raise ValueError("YOUTUBE_API_KEY environment variable is required")

# Proxy configuration
WEBSHARE_PROXY_USERNAME = os.getenv("WEBSHARE_PROXY_USERNAME")
WEBSHARE_PROXY_PASSWORD = os.getenv("WEBSHARE_PROXY_PASSWORD")

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
TRANSCRIPT_LANGUAGES = [lang.strip() for lang in os.getenv("TRANSCRIPT_LANGUAGE", "en").split(',')]

# Initialize YouTube Transcript API with proxy if credentials are available
if WEBSHARE_PROXY_USERNAME and WEBSHARE_PROXY_PASSWORD:
    logger.info("Initializing YouTubeTranscriptApi with Webshare proxy")
    youtube_transcript_api = YouTubeTranscriptApi(
        proxy_config=WebshareProxyConfig(
            proxy_username=WEBSHARE_PROXY_USERNAME,
            proxy_password=WEBSHARE_PROXY_PASSWORD,
            retries_when_blocked=50,
            # Additional configuration for Webshare proxy
            proxy_endpoints=[
                "rotating-residential.webshare.io:8001",
                "rotating-residential.webshare.io:8002",
                "rotating-residential.webshare.io:8003",
                "rotating-residential.webshare.io:8004",
                "rotating-residential.webshare.io:8005",
            ],
        )
    )
else:
    logger.info("Initializing YouTubeTranscriptApi without proxy")
    youtube_transcript_api = YouTubeTranscriptApi()

def _extract_video_id(url: str) -> str:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com\/.*[\?&]v=([a-zA-Z0-9_-]{11})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    raise ValueError("No valid YouTube video ID found in URL")

def _format_time(seconds: float) -> str:
    """Format time in seconds to MM:SS or HH:MM:SS format."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes:02d}:{seconds:02d}"

async def get_video_details(video_id: str) -> Dict[str, Any]:
    """Retrieve video details using YouTube Data API."""
    url = f"{YOUTUBE_API_BASE}/videos"
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as response:
            if response.status != 200:
                raise Exception(f"YouTube API request failed with status {response.status}")
            
            data = await response.json()
            
            if not data.get("items"):
                raise Exception("Video not found or unavailable")
            
            video_info = data["items"][0]
            snippet = video_info.get("snippet", {})
            statistics = video_info.get("statistics", {})
            content_details = video_info.get("contentDetails", {})
            
            return {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "duration": content_details.get("duration", ""),
                "view_count": statistics.get("viewCount", "0"),
                "like_count": statistics.get("likeCount", "0"),
                "comment_count": statistics.get("commentCount", "0")
            }

async def get_youtube_video_transcript(url: str) -> Dict[str, Any]:
    """
    Retrieve the transcript or video details for a given YouTube video.
    The 'start' time in the transcript is formatted as MM:SS or HH:MM:SS.
    """
    try:
        video_id = _extract_video_id(url)
        logger.info(f"Executing tool: get_video_transcript with video_id: {video_id}")
        
        try:
            # Use the initialized API with or without proxy
            raw_transcript = youtube_transcript_api.fetch(video_id, languages=TRANSCRIPT_LANGUAGES).to_raw_data()

            # Format the start time for each segment
            formatted_transcript = [
                {**segment, 'start': _format_time(segment['start'])} 
                for segment in raw_transcript
            ]

            return {
                "video_id": video_id,
                "transcript": formatted_transcript
            }
        except Exception as transcript_error:
            logger.warning(f"Error fetching transcript: {transcript_error}. Falling back to video details.")
            # Fall back to get_video_details
            video_details = await get_video_details(video_id)
            return {
                "video_id": video_id,
                "video_details": video_details,
            }
    except ValueError as e:
        logger.exception(f"Invalid YouTube URL: {e}")
        return {
            "error": f"Invalid YouTube URL: {str(e)}"
        }
    except Exception as e:
        error_message = str(e)
        logger.exception(f"Error processing video URL {url}: {error_message}")
        return {
            "error": f"Failed to process request: {error_message}"
        }

# Create the server
server = Server("youtube-mcp-server")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="get_youtube_video_transcript",
            description="Retrieve the transcript or video details for a given YouTube video. The 'start' time in the transcript is formatted as MM:SS or HH:MM:SS.",
            inputSchema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the YouTube video to retrieve the transcript/subtitles for. (e.g. https://www.youtube.com/watch?v=dQw4w9WgXcQ)",
                    },
                },
            },
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool calls."""
    if name == "get_youtube_video_transcript":
        url = arguments.get("url")
        if not url:
            return [
                types.TextContent(
                    type="text",
                    text="Error: URL parameter is required",
                )
            ]
        
        try:
            result = await get_youtube_video_transcript(url)
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]
        except Exception as e:
            logger.exception(f"Error executing tool {name}: {e}")
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: {str(e)}",
                )
            ]
    
    return [
        types.TextContent(
            type="text",
            text=f"Unknown tool: {name}",
        )
    ]

async def main():
    # Run the server using stdio transport
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())