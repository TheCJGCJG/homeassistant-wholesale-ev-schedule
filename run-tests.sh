#!/bin/bash

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Wholesale EV Schedule Test Runner ===${NC}\n"

if ! command -v docker &> /dev/null; then
    echo -e "${RED}Error: Docker is not installed or not in PATH${NC}"
    exit 1
fi

COVERAGE=false
SHELL=false
REBUILD=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --coverage|-c)
            COVERAGE=true
            shift
            ;;
        --shell|-s)
            SHELL=true
            shift
            ;;
        --rebuild|-r)
            REBUILD=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  -c, --coverage    Generate HTML coverage report"
            echo "  -s, --shell       Open interactive shell in container"
            echo "  -r, --rebuild     Force rebuild of Docker image"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

IMAGE=wholesale-ev-schedule-test

if [ "$REBUILD" = true ] || ! docker images | grep -q "$IMAGE"; then
    echo -e "${BLUE}Building Docker image...${NC}"
    docker build -t "$IMAGE" .
    echo -e "${GREEN}Image built successfully${NC}\n"
else
    echo -e "${GREEN}Using existing Docker image${NC}\n"
fi

MOUNTS=(
    -v "$(pwd)/custom_components:/app/custom_components"
    -v "$(pwd)/tests:/app/tests"
    -v "$(pwd)/pytest.ini:/app/pytest.ini"
)

if [ "$SHELL" = true ]; then
    echo -e "${BLUE}Opening interactive shell...${NC}"
    docker run --rm -it "${MOUNTS[@]}" "$IMAGE" /bin/bash
elif [ "$COVERAGE" = true ]; then
    echo -e "${BLUE}Running tests with coverage...${NC}\n"
    docker run --rm "${MOUNTS[@]}" -v "$(pwd)/htmlcov:/app/htmlcov" "$IMAGE" \
        pytest --cov=custom_components/wholesale_ev_schedule --cov-report=term-missing --cov-report=html
    echo -e "\n${GREEN}Coverage report: htmlcov/index.html${NC}"
else
    echo -e "${BLUE}Running tests...${NC}\n"
    docker run --rm "${MOUNTS[@]}" "$IMAGE"
    echo -e "\n${GREEN}All tests passed!${NC}"
fi
