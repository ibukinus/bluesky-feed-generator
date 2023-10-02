FROM python:3.11 as builder

ENV PYTHONUNBUFFERED=1
ENV PATH="$PATH:/root/.cargo/bin"

WORKDIR /app

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y


COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

RUN sudachipy ubuild -s /usr/local/lib/python3.11/site-packages/sudachidict_core/resources/system.dic user.csv \
    && mv user.dic /usr/local/lib/python3.11/site-packages/sudachipy/resources/ \
    && cp sudachi.json /usr/local/lib/python3.11/site-packages/sudachipy/resources/


FROM python:3.11-slim as production

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn
COPY --from=builder /app /app/

EXPOSE 8000
ENTRYPOINT ["gunicorn", "-w", "2", "-b", "0.0.0.0", "server.app:app"]