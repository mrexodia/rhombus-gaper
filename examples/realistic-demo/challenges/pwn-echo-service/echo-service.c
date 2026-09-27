#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const char decoy[] = "rhombus{strings_is_only_the_beginning}";

__attribute__((used, noinline))
void diagnostic_console(void) {
    char flag[128] = {0};
    FILE *file = fopen("/flag.txt", "r");
    if (file == NULL) {
        puts("Diagnostic data is unavailable.");
        exit(1);
    }
    if (fgets(flag, sizeof(flag), file) != NULL) {
        puts(flag);
    }
    fclose(file);
}

__attribute__((noinline))
void echo_request(void) {
    char request[64];
    puts("Acme Echo Service v1.7");
    puts("Enter a diagnostic message:");
    /* Intentionally vulnerable for the local CTF challenge. */
    read(STDIN_FILENO, request, 256);
    puts("Message recorded.");
}

int main(void) {
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);
    if (getenv("PRINT_TEST_FIXTURE") != NULL) {
        puts(decoy);
    }
    echo_request();
    return 0;
}
