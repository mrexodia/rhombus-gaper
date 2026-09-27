FROM gcc:14-bookworm AS builder
WORKDIR /src
COPY echo-service.c .
RUN gcc -O0 -g0 -fno-stack-protector -fno-pie -no-pie -Wl,-z,noexecstack -o /echo-service echo-service.c

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 ctf
COPY --from=builder /echo-service /app/echo-service
COPY flag.txt /flag.txt
RUN chmod 0555 /app/echo-service && chmod 0444 /flag.txt
USER ctf
EXPOSE 1337
CMD ["socat", "TCP-LISTEN:1337,reuseaddr,fork", "EXEC:/app/echo-service,stderr"]
