# FROM public.ecr.aws/lambda/python:3.11

# COPY ./pyproject.toml ./poetry.lock ./
# RUN pip install 'uvicorn[standard]'

# ENV POETRY_REQUESTS_TIMEOUT=10800
# RUN python -m pip install --upgrade pip && \
#     pip install poetry --no-cache-dir && \
#     poetry config virtualenvs.create false && \
#     poetry install --no-interaction --no-ansi --only main && \
#     pip install websockets && \
#     poetry cache clear --all pypi

# COPY ./app ./app
# COPY ./embedding_statemachine ./embedding_statemachine

# CMD ["app.websocket.handler"]


FROM public.ecr.aws/lambda/python:3.11

# Attempt to add a reliable DNS server
RUN echo "nameserver 8.8.8.8" > /etc/resolv.conf

COPY ./pyproject.toml ./poetry.lock ./
RUN pip install 'uvicorn[standard]' && pip install websockets

ENV POETRY_REQUESTS_TIMEOUT=10800
RUN python -m pip install --upgrade pip && \
    pip install poetry --no-cache-dir && \
    poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --only main && \
    poetry cache clear --all pypi

COPY ./app ./app
COPY ./embedding_statemachine ./embedding_statemachine

CMD ["app.websocket.handler"]
