#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char decoy[] = "rhombus{percent_x_is_not_the_whole_forecast}";

int main(void) {
    char station[256] = {0};
    char secret[128] = {0};
    FILE *file;

    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);

    file = fopen("/flag.txt", "r");
    if (file == NULL || fgets(secret, sizeof(secret), file) == NULL) {
        puts("Telemetry secret unavailable.");
        return 1;
    }
    fclose(file);

    puts("Signal Ridge Weather Relay");
    puts("Station identifier:");
    if (fgets(station, sizeof(station), stdin) == NULL) {
        return 1;
    }
    station[strcspn(station, "\n")] = '\0';
    printf("Forecast requested for ");
    /* Intentionally vulnerable for this local CTF challenge. */
    printf(station);
    puts("");

    if (getenv("SHOW_TEST_FIXTURE") != NULL) {
        puts(decoy);
    }
    memset(secret, 0, sizeof(secret));
    return 0;
}
