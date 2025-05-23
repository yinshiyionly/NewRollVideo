FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS build-env

# 非交互式
ENV DEBIAN_FRONTEND=noninteractive

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    cmake \
    git \
    vim \
    clang \
    llvm \
    clang-tools \
    libtool \
    autoconf \
    automake \
    pkg-config \
    yasm \
    nasm \
    zlib1g-dev \
    libx264-dev \
    libx265-dev \
    libvpx-dev \
    libfdk-aac-dev \
    libsdl2-dev \
    libass-dev \
    libva-dev \
    libvdpau-dev \
    libxcb1-dev \
    libxcb-shm0-dev \
    libxcb-xfixes0-dev \
    nvidia-driver-525-server \
    libreadline-dev \
    libbz2-dev \
    libsm6 \
    libxrender1 \
    libxext-dev \
    libgomp1 \
    liblzma-dev \
    libgl1-mesa-glx \
    libprotobuf-dev \
    protobuf-compiler \
    libglib2.0-0 \
    mpich \
    openmpi-bin \
    libopenmpi-dev \
    gcc \
    g++ \
    make \
    zlib1g \
    openssl \
    libsqlite3-dev \
    libssl-dev \
    libffi-dev \
    unzip \
    pciutils \
    net-tools \
    libblas-dev \
    gfortran \
    libblas3 \
    libopenblas-dev \
    libswresample-dev \
    libfreetype6-dev \
    libtheora-dev \
    libvorbis-dev \
    texinfo \
    libmp3lame-dev \
    libopus-dev \
    # 安装 CUDA 开发工具
    cuda-nvcc-12-4 \
    cuda-nvrtc-12-4 \
    cuda-nvrtc-dev-12-4 \
    cuda-nvml-dev-12-4 \
    cuda-command-line-tools-12-4 \
    cuda-libraries-dev-12-4 \
    supervisor \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 安装cuDNN
RUN cd /tmp && \
    wget https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/linux-x86_64/cudnn-linux-x86_64-8.9.4.25_cuda12-archive.tar.xz && \
    tar -xf cudnn-linux-x86_64-8.9.4.25_cuda12-archive.tar.xz && \
    cp -r cudnn-linux-x86_64-8.9.4.25_cuda12-archive/lib/* /usr/local/cuda/lib64/ && \
    cp -r cudnn-linux-x86_64-8.9.4.25_cuda12-archive/include/* /usr/local/cuda/include/ && \
    rm -rf /tmp/cudnn* && \
    ldconfig

# 下载并安装nv-codec-headers 注意显卡驱动版本
RUN git clone https://github.com/FFmpeg/nv-codec-headers.git && \
    cd nv-codec-headers && \
    git checkout sdk/12.0 && \
    make -j$(nproc) && \
    make install

# 下载并编译支持NVIDIA硬件加速的FFmpeg
RUN cd /tmp && \
    wget https://ffmpeg.org/releases/ffmpeg-7.1.tar.gz && \
    tar xvf ffmpeg-7.1.tar.gz && \
    cd ffmpeg-7.1 && \
    PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:/usr/local/cuda/lib64/pkgconfig" \
    ./configure \
    --prefix=/usr/local \
    --disable-debug \
    --disable-doc \
    --disable-ffplay \
    --enable-shared \
    --enable-gpl \
    --enable-nonfree \
    --enable-libfdk-aac \
    --enable-libx264 \
    --enable-libx265 \
    --enable-cuda \
    --enable-cuda-llvm \
    --enable-cuda-nvcc \
    --enable-libfreetype \
    --enable-libharfbuzz \
    --enable-libfontconfig \
    --enable-cuvid \
    --enable-nvenc \
    --enable-filter=scale_cuda \
    --enable-filter=scale_npp \
    --enable-filter=thumbnail_cuda \
    --enable-filter=overlay_cuda \
    --enable-filter=yadif_cuda \
    --enable-filter=bwdif_cuda \
    --enable-filter=scale \
    --enable-filter=crop \
    --enable-libnpp \
    --enable-pthreads \
    --extra-cflags="-I/usr/local/cuda/include -I/usr/local/include" \
    --extra-ldflags="-L/usr/local/cuda/lib64 -L/usr/local/lib" \
    --extra-libs="-lpthread -lm -lcuda -lcudart" && \
    make -j$(nproc) && \
    make install


FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# 安装 Python 3.10 和 pip
RUN apt-get update && apt-get install -y \
    software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.10 python3.10-dev python3.10-distutils python3-pip && \
    ln -sf python3.10 /usr/bin/python3 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 拷贝 FFmpeg 和相关依赖
# COPY --from=build-env /ffmpeg-install /usr/local
COPY --from=build-env /usr/local/cuda /usr/local/cuda
COPY --from=build-env /usr/local/lib /usr/local/lib
COPY --from=build-env /usr/local/include /usr/local/include

# 安装 Python 依赖
COPY dockerfile-requirements.txt /app/
WORKDIR /app
RUN pip3 install --no-cache-dir -r dockerfile-requirements.txt

ENV PATH="/usr/local/cuda/bin:/usr/local/bin:${PATH}" \
    LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/local/lib:${LD_LIBRARY_PATH}" \
    CUDA_HOME="/usr/local/cuda" \
    FFMPEG_BINARY="/usr/local/bin/ffmpeg" \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility,graphics

EXPOSE 6000

CMD ["tail", "-f", "/dev/null"]
