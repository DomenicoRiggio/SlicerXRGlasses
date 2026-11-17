set(MODULE_NAME XRGlasses)

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  ${MODULE_NAME}Lib/__init__.py
  ${MODULE_NAME}Lib/cameras.py
  ${MODULE_NAME}Lib/device.py
  ${MODULE_NAME}Lib/displays.py
  ${MODULE_NAME}Lib/macdisplay.py
  ${MODULE_NAME}Lib/sdk.py
  ${MODULE_NAME}Lib/session.py
  ${MODULE_NAME}Lib/settings.py
  ${MODULE_NAME}Lib/stereo_math.py
  ${MODULE_NAME}Lib/toolbar.py
  ${MODULE_NAME}Lib/usb.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Testing/test_stereo_math.py
  )

# Pure Python: nothing is compiled. The glasses vendor SDK is loaded at run
# time from the folder the user selects in the module panel.
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

if(BUILD_TESTING)
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)
endif()
