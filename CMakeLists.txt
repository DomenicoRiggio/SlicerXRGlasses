cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(SlicerXRGlasses)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGEURL "https://github.com/DomenicoRiggio/SlicerXRGlasses")
set(EXTENSION_CONTRIBUTORS "Domenico Riggio, Steve Pieper, Edoardo De Rose")
set(EXTENSION_DESCRIPTION "Stereoscopic view of the Slicer 3D scene in XR glasses, with optional 6DoF head tracking. Currently supports VITURE XR glasses; uses the VITURE XR Glasses SDK, free to download from https://www.viture.com/developer. Research software; not for clinical use.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/DomenicoRiggio/SlicerXRGlasses/main/XRGlasses/Resources/Icons/XRGlasses.png")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA")
set(EXTENSION_CATEGORY "Virtual Reality")

#-----------------------------------------------------------------------------
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

add_subdirectory(XRGlasses)

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
