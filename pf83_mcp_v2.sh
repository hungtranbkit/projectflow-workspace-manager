#!/bin/sh
add() {
    printf '%s\n' "$(($1 + $2))"
}

[ "$(add 2 3)" = 5 ] || exit 1
[ "$(add -2 3)" = 1 ] || exit 1
[ "$(add 0 0)" = 0 ] || exit 1
printf '%s\n' 'PF83_MCP_V2_BUILD_TEST_PASS'
