#include <algorithm>
#include <cstdio>
#include <sstream>
#include <vector>

#include <kungfu/cuda/stream.hpp>
#include <kungfu/nccl/gpu_collective.hpp>
#include <kungfu/python/init.h>
#include <kungfu/utils/trace.hpp>

#include <nccl.h>

struct show_nccl_error {
    std::string operator()(ncclResult_t err) const
    {
        return std::to_string(static_cast<int>(err)) + ": " +
               ncclGetErrorString(err);
    }
};

using nccl_checker = error_checker<ncclResult_t, ncclSuccess, show_nccl_error>;

void kungfu_show_cuda_version()
{
    int driverVersion;
    KUNGFU_CHECK(kungfu::cuda_checker) << cudaDriverGetVersion(&driverVersion);
    printf("CUDA Driver Veresion: %d\n", driverVersion);

    int runtimeVersion;
    KUNGFU_CHECK(kungfu::cuda_checker)
        << cudaRuntimeGetVersion(&runtimeVersion);
    printf("CUDA Runtime Veresion: %d\n", runtimeVersion);
}

void kungfu_show_nccl_version()
{
    int version;
    KUNGFU_CHECK(nccl_checker) << ncclGetVersion(&version);
    printf("NCCL Version: %d\n", version);
}

template <typename T>
struct nccl_type;
template <>
struct nccl_type<int32_t> {
    static ncclDataType_t value() { return ncclInt32; }
};
template <>
struct nccl_type<kungfu::float16> {
    static ncclDataType_t value() { return ncclFloat16; }
};
template <>
struct nccl_type<float> {
    static ncclDataType_t value() { return ncclFloat; }
};

namespace kungfu
{
ncclDataType_t to_nccl_type(const KungFu_Datatype dtype)
{
    switch (dtype) {
    case type_encoder::value<int32_t>():
        return nccl_type<int32_t>::value();
    case type_encoder::value<kungfu::float16>():
        return nccl_type<kungfu::float16>::value();
    case type_encoder::value<float>():
        return nccl_type<float>::value();
    default:
        // TODO: add more types
        throw std::invalid_argument("unsupported dtype");
    }
}

class gpu_collective_nccl : public gpu_collective
{
    ncclComm_t comm;
    const int _root;
    const int _rank;
    const int _cluster_size;

    CudaStream _stream;

  public:
    gpu_collective_nccl(ncclUniqueId id, int cluster_size, int rank, int root)
        : _root(root), _rank(rank), _cluster_size(cluster_size)
    {
        KUNGFU_CHECK(nccl_checker)
            << ncclCommInitRank(&comm, cluster_size, id, rank);
    }

    ~gpu_collective_nccl()
    {
        KUNGFU_CHECK(nccl_checker) << ncclCommDestroy(comm);
    }

    void reduce(const void *send_buf, void *recv_buf, size_t count,
                KungFu_Datatype dtype)
    {
        TRACE_SCOPE(__func__);
        // https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/api/colls.html#ncclreduce
        KUNGFU_CHECK_HINT(nccl_checker, __func__)
            << ncclReduce(send_buf, recv_buf, count, to_nccl_type(dtype),
                          ncclSum, _root, comm, _stream);
        _stream.sync();
    }

    void broadcast(const void *send_buf, void *recv_buf, size_t count,
                   KungFu_Datatype dtype)
    {
        TRACE_SCOPE(__func__);
        // https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/api/colls.html#ncclbroadcast
        KUNGFU_CHECK_HINT(nccl_checker, __func__)
            << ncclBroadcast(send_buf, recv_buf, count, to_nccl_type(dtype),
                             _root, comm, _stream);
        _stream.sync();
    }

    void all_reduce(const void *send_buf, void *recv_buf, size_t count,
                    KungFu_Datatype dtype)
    {
        TRACE_SCOPE(__func__);
        // https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/api/colls.html#ncclallreduce
        KUNGFU_CHECK_HINT(nccl_checker, __func__)
            << ncclAllReduce(send_buf, recv_buf, count, to_nccl_type(dtype),
                             ncclSum, comm, _stream);
        _stream.sync();
    }
};

gpu_collective *new_global_gpu_collective(kungfu::Peer &self)
{
    ncclUniqueId id;
    const int root = 0;
    const int rank = self.Rank();
    KUNGFU_CHECK(cuda_checker) << cudaSetDevice(kungfu_get_cuda_index());
    if (rank == root) { KUNGFU_CHECK(nccl_checker) << ncclGetUniqueId(&id); }
    self.Broadcast(&id, &id, sizeof(id), type_encoder::value<uint8_t>(),
                   "nccl id");
    return new gpu_collective_nccl(id, self.Size(), rank, root);
}

gpu_collective *new_local_gpu_collective(kungfu::Peer &self)
{
    ncclUniqueId id;
    const int root = 0;
    const int rank = self.LocalRank();
    KUNGFU_CHECK(cuda_checker) << cudaSetDevice(kungfu_get_cuda_index());
    if (rank == root) { KUNGFU_CHECK(nccl_checker) << ncclGetUniqueId(&id); }
    self.LocalBroadcast(&id, &id, sizeof(id), type_encoder::value<uint8_t>(),
                        "local nccl id");
    return new gpu_collective_nccl(id, self.LocalSize(), rank, root);
}
}  // namespace kungfu
