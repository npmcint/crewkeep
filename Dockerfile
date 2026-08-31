FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every top-level module MUST be listed here (feedback.py lesson from xjobs:
# omitting one crashes the container with ModuleNotFoundError -> 502).
COPY auth.py db.py resume.py llm.py screening.py app.py crewkeep.py ./
COPY web/ web/

ENV CREWKEEP_HOST=0.0.0.0
ENV CREWKEEP_PORT=8091

EXPOSE 8091
CMD ["python", "crewkeep.py", "serve"]
