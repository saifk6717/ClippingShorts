# AI worker: Chirp 3 transcription, the two Gemini judges, and YouTube publishing.
# Built from the shared backend image (same code, FFmpeg and fonts everywhere): see docker-compose.yml.
FROM backend_image
CMD ["python", "-m", "clippingshorts.workers.ai_worker"]
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=3 CMD ["python", "-m", "clippingshorts.workers.healthcheck", "ai"]
