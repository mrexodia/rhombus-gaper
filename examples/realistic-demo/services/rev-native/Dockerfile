FROM gcc:14-bookworm AS builder
WORKDIR /src
COPY badge-checker.c .
RUN gcc -O1 -fno-pie -no-pie -o /badge-checker badge-checker.c && strip /badge-checker

FROM scratch
COPY --from=builder /badge-checker /badge-checker
