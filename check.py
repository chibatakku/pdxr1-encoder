import os, clr
K = r"C:\Program Files\Thorlabs\Kinesis"

# Kinesis フォルダを DLL の検索先に追加
os.add_dll_directory(K)
os.environ["PATH"] = K + ";" + os.environ["PATH"]

clr.AddReference(K + r"\Thorlabs.MotionControl.DeviceManagerCLI.dll")
clr.AddReference(K + r"\Thorlabs.MotionControl.Benchtop.PiezoCLI.dll")
from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI  # type: ignore
from Thorlabs.MotionControl.Benchtop.PiezoCLI.PDXC2 import InertiaStageController  # type: ignore

DeviceManagerCLI.BuildDeviceList()
print("All  :", list(DeviceManagerCLI.GetDeviceList()))
print("PDXC2:", list(DeviceManagerCLI.GetDeviceList(InertiaStageController.DevicePrefix_PDXC2)))