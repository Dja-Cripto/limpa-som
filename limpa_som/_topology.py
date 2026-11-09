"""Definições COM da API Device Topology do Windows (o pycaw não traz essas interfaces).

Usadas para achar o controle 'Aumento de sensibilidade do microfone' (mic boost) das placas
de som, que não aparece no volume principal do Windows.
"""
from ctypes import HRESULT, POINTER, c_float, c_int, c_void_p
from ctypes.wintypes import BOOL, DWORD, LPWSTR, UINT

from comtypes import COMMETHOD, GUID, IUnknown


class IControlInterface(IUnknown):
    _iid_ = GUID("{45D37C3F-5140-444A-AE24-400789F3CBF3}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetName", (["out"], POINTER(LPWSTR), "n")),
        COMMETHOD([], HRESULT, "GetIID", (["out"], POINTER(GUID), "iid")),
    )


class IPart(IUnknown):
    _iid_ = GUID("{AE2DE0E4-5BCA-4F2D-AA46-5D13F8FDB3A9}")


class IPartsList(IUnknown):
    _iid_ = GUID("{6DAA848C-5EB0-45CC-AEA5-998A2CDA1FFB}")


class IDeviceTopology(IUnknown):
    _iid_ = GUID("{2A07407E-6497-4A18-9787-32F79BD0D98F}")


class ISubunit(IUnknown):
    _iid_ = GUID("{82149A85-DBA6-4487-86BB-EA8F7FEFCC71}")


class IConnector(IUnknown):
    _iid_ = GUID("{9C2C4058-23F5-41DE-877A-DF3AF236A09E}")


IPartsList._methods_ = (
    COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(UINT), "c")),
    COMMETHOD([], HRESULT, "GetPart", (["in"], UINT, "i"), (["out"], POINTER(POINTER(IPart)), "p")),
)

IPart._methods_ = (
    COMMETHOD([], HRESULT, "GetName", (["out"], POINTER(LPWSTR), "n")),
    COMMETHOD([], HRESULT, "GetLocalId", (["out"], POINTER(UINT), "id")),
    COMMETHOD([], HRESULT, "GetGlobalId", (["out"], POINTER(LPWSTR), "id")),
    COMMETHOD([], HRESULT, "GetPartType", (["out"], POINTER(c_int), "t")),
    COMMETHOD([], HRESULT, "GetSubType", (["out"], POINTER(GUID), "g")),
    COMMETHOD([], HRESULT, "GetControlInterfaceCount", (["out"], POINTER(UINT), "c")),
    COMMETHOD([], HRESULT, "GetControlInterface", (["in"], UINT, "i"),
              (["out"], POINTER(POINTER(IControlInterface)), "ci")),
    COMMETHOD([], HRESULT, "EnumPartsIncoming", (["out"], POINTER(POINTER(IPartsList)), "pl")),
    COMMETHOD([], HRESULT, "EnumPartsOutgoing", (["out"], POINTER(POINTER(IPartsList)), "pl")),
    COMMETHOD([], HRESULT, "GetTopologyObject", (["out"], POINTER(POINTER(IDeviceTopology)), "t")),
    COMMETHOD([], HRESULT, "Activate", (["in"], DWORD, "ctx"), (["in"], POINTER(GUID), "iid"),
              (["out"], POINTER(POINTER(IUnknown)), "o")),
    COMMETHOD([], HRESULT, "RegisterControlChangeCallback", (["in"], POINTER(GUID), "iid"),
              (["in"], c_void_p, "n")),
    COMMETHOD([], HRESULT, "UnregisterControlChangeCallback", (["in"], c_void_p, "n")),
)

IConnector._methods_ = (
    COMMETHOD([], HRESULT, "GetType", (["out"], POINTER(c_int), "t")),
    COMMETHOD([], HRESULT, "GetDataFlow", (["out"], POINTER(c_int), "f")),
    COMMETHOD([], HRESULT, "ConnectTo", (["in"], POINTER(IConnector), "c")),
    COMMETHOD([], HRESULT, "Disconnect"),
    COMMETHOD([], HRESULT, "IsConnected", (["out"], POINTER(BOOL), "b")),
    COMMETHOD([], HRESULT, "GetConnectedTo", (["out"], POINTER(POINTER(IConnector)), "c")),
    COMMETHOD([], HRESULT, "GetConnectorIdConnectedTo", (["out"], POINTER(LPWSTR), "id")),
    COMMETHOD([], HRESULT, "GetDeviceIdConnectedTo", (["out"], POINTER(LPWSTR), "id")),
)

IDeviceTopology._methods_ = (
    COMMETHOD([], HRESULT, "GetConnectorCount", (["out"], POINTER(UINT), "c")),
    COMMETHOD([], HRESULT, "GetConnector", (["in"], UINT, "i"), (["out"], POINTER(POINTER(IConnector)), "c")),
    COMMETHOD([], HRESULT, "GetSubunitCount", (["out"], POINTER(UINT), "c")),
    COMMETHOD([], HRESULT, "GetSubunit", (["in"], UINT, "i"), (["out"], POINTER(POINTER(ISubunit)), "s")),
    COMMETHOD([], HRESULT, "GetPartById", (["in"], UINT, "id"), (["out"], POINTER(POINTER(IPart)), "p")),
    COMMETHOD([], HRESULT, "GetDeviceId", (["out"], POINTER(LPWSTR), "id")),
    COMMETHOD([], HRESULT, "GetSignalPath", (["in"], POINTER(IPart), "a"), (["in"], POINTER(IPart), "b"),
              (["in"], BOOL, "rej"), (["out"], POINTER(POINTER(IPartsList)), "pl")),
)


class IPerChannelDbLevel(IUnknown):
    _iid_ = GUID("{C2F8E001-F205-4BC9-99BC-C13B1E048CCB}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(UINT), "c")),
        COMMETHOD([], HRESULT, "GetLevelRange", (["in"], UINT, "ch"), (["out"], POINTER(c_float), "mn"),
                  (["out"], POINTER(c_float), "mx"), (["out"], POINTER(c_float), "st")),
        COMMETHOD([], HRESULT, "GetLevel", (["in"], UINT, "ch"), (["out"], POINTER(c_float), "lv")),
        COMMETHOD([], HRESULT, "SetLevel", (["in"], UINT, "ch"), (["in"], c_float, "lv"),
                  (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "SetLevelUniform", (["in"], c_float, "lv"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "SetLevelAllChannels", (["in"], c_void_p, "lv"), (["in"], UINT, "n"),
                  (["in"], POINTER(GUID), "ctx")),
    )


class IAudioVolumeLevel(IPerChannelDbLevel):
    _iid_ = GUID("{7FB7B48F-531D-44A2-BCB3-5AD5A134B3DC}")
    _methods_ = ()
