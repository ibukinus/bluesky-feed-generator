FROM python:3.11 as builder

ENV PYTHONUNBUFFERED=1
ENV PATH="$PATH:/root/.cargo/bin"

WORKDIR /app

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY sudachi.json user.csv ./

RUN sudachipy ubuild -s /usr/local/lib/python3.11/site-packages/sudachidict_core/resources/system.dic user.csv \
    && mv user.dic /usr/local/lib/python3.11/site-packages/sudachipy/resources/ \
    && cp sudachi.json /usr/local/lib/python3.11/site-packages/sudachipy/resources/


FROM python:3.11-slim as runner

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd -r feedgen && useradd -r -g feedgen feedgen
USER feedgen

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn
COPY --chown=feedgen:feedgen . .

EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0", "server.app:app"]
