FROM gcc:14-bookworm AS builder
WORKDIR /src
COPY format-forecast.c .
RUN gcc -O0 -g0 -fno-stack-protector -fno-pie -no-pie -Wl,-z,noexecstack -o /format-forecast format-forecast.c

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10002 ctf
COPY --from=builder /format-forecast /app/format-forecast
COPY flag.txt /flag.txt
RUN chmod 0555 /app/format-forecast && chmod 0444 /flag.txt
USER ctf
EXPOSE 1338
CMD ["socat", "TCP-LISTEN:1338,reuseaddr,fork", "EXEC:/app/format-forecast,stderr"]
