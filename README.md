# RecoveryOS

Autonomous operations system designed to preserve business outcomes through agentic failure recovery.

## Overview

RecoveryOS is an event-driven, multi-agent operations platform built with Google ADK, Gemini, and Google Cloud services. When standard operational procedures encounter unexpected faults (service outages, schema drift, contradictory evidence), RecoveryOS diagnoses the failure, identifies compliant alternatives, enforces deterministic safety policies, and independently verifies business outcomes before completion.

## Current Status

- **Core Engine & Data Models**: Implemented with lifecycle states and outcome contract models.
- **Simulation Layer**: Simulated external services for customer onboarding with failure injection.
- **Policy Engine**: Deterministic Python rules enforcing ordering, authorization, and constraints.
- **Agent Integration**: ADK-based Taskmaster and Recovery Specialist definitions.
- **Outcome Verification & Step Tracking**: Real-time step execution tracking and independent outcome verification.

## Local Setup

### 1. Prerequisites
- Python 3.11+
- Virtual environment

### 2. Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Environment Configuration
Copy `.env.example` to `.env` and configure your credentials:
```bash
cp .env.example .env
```
Ensure `GOOGLE_API_KEY` is set with your Gemini API key.

## Running Tests

Run the test suite with pytest:
```bash
pytest
```

To run with verbose output:
```bash
pytest -v
```

## Running the Server

Start the FastAPI application:
```bash
python -m backend
```
The server will start at `http://localhost:8000`.
