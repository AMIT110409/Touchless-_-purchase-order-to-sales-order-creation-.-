import os
import json
import base64
import anthropic
from typing import Optional, Dict, Any, List


class ClaudeAPIClient:
    """
    Claude API Client for extracting structured data from documents.
    Supports both text-based extraction and vision (image) extraction.
    """
    def __init__(self, api_key: Optional[str] = None, model: str = "claude-sonnet-4-6"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in environment or provided.")

        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = model

    def extract_data(self, prompt: str, system_prompt: str = "") -> str:
        """
        Sends a text prompt to Claude and returns the generated text.
        Used for text-based PDFs where native text has been extracted.
        """
        import time
        max_retries = 3
        for attempt in range(max_retries):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    temperature=0,
                    system=system_prompt,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": prompt
                                }
                            ]
                        }
                    ]
                )
                return message.content[0].text
            except Exception as e:
                print(f"Claude API Error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                else:
                    raise

    def extract_from_images(
        self,
        image_bytes_list: List[bytes],
        system_prompt: str = "",
        extra_text: str = "",
        media_type: str = "image/png",
    ) -> str:
        """
        Vision extraction: send one or more page images to Claude and return extracted text.

        Used for:
          - Scanned / image-based PDFs (no selectable text)
          - PNG / JPEG attachments from emails
          - Inline images embedded in HTML email bodies

        Args:
            image_bytes_list: List of raw image bytes (PNG or JPEG) — one per PDF page / image.
            system_prompt:    System prompt (same as text extraction).
            extra_text:       Optional text instruction appended after the images.
            media_type:       MIME type for all images: "image/png" or "image/jpeg".
        """
        import time
        content_blocks = []

        for img_bytes in image_bytes_list:
            b64_data = base64.standard_b64encode(img_bytes).decode("utf-8")
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data,
                },
            })

        # Append extraction instruction text after the images
        instruction = extra_text or (
            "Extract the Purchase Order data from the image(s) above "
            "according to the system instructions. Output ONLY the JSON object."
        )
        content_blocks.append({"type": "text", "text": instruction})

        max_retries = 3
        for attempt in range(max_retries):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    temperature=0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                return message.content[0].text
            except Exception as e:
                print(f"Claude Vision API Error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 * (attempt + 1))
                else:
                    raise


def get_claude_client() -> ClaudeAPIClient:
    """Helper to get a client instance using env vars."""
    return ClaudeAPIClient()
