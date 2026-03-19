#/bin/sh

#support macos zsh and bash
ECHO=$([[ "$SHELL" == *"zsh"* ]] && echo "echo" || echo "echo -e")

# Define color variables
COLOR_BLUE="\033[0;36m"
COLOR_GREEN="\033[0;32m"
COLOR_RED="\033[0;31m"
COLOR_OFF="\033[0m"

$ECHO "${COLOR_GREEN}Setting up Python/Cython test environment... ${COLOR_OFF}"
PYTHON_VERSION="3.14t"  # Free-threaded build (nogil, PEP 779)
CYTHON_VERSION="3.2.4"  # Full free-threading support (pymutex, critical sections)

PLATFORM="$(uname -s)"
case "${PLATFORM}" in
    Linux*)     PLATFORM=Linux;;
    Darwin*)    PLATFORM=Mac;;
    *)          PLATFORM="UNKNOWN:${PLATFORM}"
esac
$ECHO "Detected platform: ${COLOR_GREEN}$PLATFORM${COLOR_OFF}"

# Ensure python3 and pip3 are installed
if [ "${PLATFORM}" = "Linux" ]; then
    if [ "${EUID}" -ne 0 ]; then
        SUDO=sudo
    fi
    ${SUDO} apt-get install -y python3 python3-pip python3-venv clang
elif [ "${PLATFORM}" = "Mac" ]; then
    brew install python3
else
    $ECHO "${COLOR_RED}[ ERROR ] Unrecognised platform: ${PLATFORM}${COLOR_OFF}"
    exit 1
fi

VENV_DIR=".venv"
UV_PIP_INSTALL="uv pip install --no-config --upgrade"
export UV_PYTHON_INSTALL_DIR="${VENV_DIR}"

if [ ! -d "${VENV_DIR}" ]; then
    [ -f ".python-version" ] && rm -f ".python-version"
    python3 -m venv --prompt "pip" "${VENV_DIR}"
    source "${VENV_DIR}/bin/activate"
    python3 -m pip install --upgrade pip
    python3 -m pip install --upgrade uv
    uv venv --prompt "uv" \
        --seed --relocatable \
        --allow-existing --no-config \
        --python-preference only-managed \
        --python "${PYTHON_VERSION}" \
        "${VENV_DIR}"
fi

source ./.venv/bin/activate
if ! command -v "uv" >/dev/null 2>&1 ; then
    $ECHO "${COLOR_RED}[ ERROR ] Failed to install uv... ${COLOR_OFF}"
    exit -1
fi

# Ensure uv is upgraded to latest
${UV_PIP_INSTALL} uv

# Install desired python verion using uv
uv python install --no-config "${PYTHON_VERSION}"
uv python pin --no-config "${PYTHON_VERSION}"

# Install required packages
${UV_PIP_INSTALL} build wheel setuptools
#${UV_PIP_INSTALL} "git+https://github.com/cython/cython.git"
${UV_PIP_INSTALL} "cython==${CYTHON_VERSION}"
${UV_PIP_INSTALL} pytest pytest-asyncio pytest-cov pytest-xdist
${UV_PIP_INSTALL} qh3

version=$(python --version 2>&1 | awk '{print $NF}')
if [ $? -ne 0 ]; then
    $ECHO "${COLOR_RED}Python failed to install${COLOR_OFF}"
    exit 1
fi
$ECHO "Python: ${COLOR_GREEN}${version}${COLOR_OFF}"
version=$(cython --version 2>&1 | awk '{print $NF}')
if [ $? -ne 0 ]; then
    $ECHO "${COLOR_RED}Cython failed to install${COLOR_OFF}"
    exit 1
fi
$ECHO "Cython: ${COLOR_GREEN}${version}${COLOR_OFF}"

cd "${VENV_DIR}"
PYTHON_DIR=$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("prefix").split("/")[-1])')
if [ -d "include" ] && [ ! -L "include" ]; then
    rm -rf "include"
fi
ln -sf "${PYTHON_DIR}/include" "include"
cd ..
deactivate

$ECHO "Python/Cython environment set up: ${COLOR_GREEN}SUCCESS${COLOR_OFF}"
$ECHO "activate with: ${COLOR_BLUE}source ${VENV_DIR}/bin/activate${COLOR_OFF}"